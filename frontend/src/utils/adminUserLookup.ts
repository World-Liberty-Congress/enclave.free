import { API_BASE, STORAGE_KEYS } from '../types/onboarding';
import {
  decryptField,
  hasNip04Support,
  type EncryptedField,
} from './encryption';

export type Selector = string;
type Predicate = {
  field: Selector;
  operator: 'equals' | 'contains' | 'gt' | 'gte' | 'lt' | 'lte';
  value: string | number | boolean;
};
type Metadata = {
  approved?: boolean;
  user_type_id?: number;
  pubkey_present?: boolean;
};
type Args = {
  user_ids?: number[];
  select: Selector[];
  where?: Predicate[];
  metadata?: Metadata;
  limit?: number;
  cursor?: string;
};
export type LookupRequest = {
  version: 1;
  conversation_id: string;
  turn_id: string;
  call_id: string;
  nonce: string;
  session_fingerprint: string;
  admin_pubkey: string;
  expires_at: number;
  operation: 'read_users' | 'search_users';
  args: Args;
};
type Cell =
  | { status: 'ok'; value: string; excerpt: boolean }
  | { status: 'missing' | 'unreadable' };
type Row = {
  id: number;
  status: 'ok' | 'missing_user';
  fields: Record<string, Cell>;
};
type Coverage = {
  scanned: number;
  eligible: number;
  unreadable: number;
  complete: boolean;
};
export type LookupResult =
  | { status: 'error'; code: string }
  | {
      status: 'ok';
      users: Row[];
      coverage: Coverage;
      matched_so_far: number;
      matched_count: number | null;
      next_scan: string | null;
      next_page: string | null;
      consistency: 'live_id_window';
    };
type Projection = {
  users: {
    id: number;
    values: Record<string, EncryptedField | { value: string } | null>;
  }[];
  fields: { selector: string; type: string }[];
  through_id: number;
  next_after_id: number | null;
  eligible: number;
};
type ScanState = {
  key: string;
  pending: number[];
  after: number;
  through?: number;
  coverage: Coverage;
  matched: number;
};
class LookupFailure extends Error {
  constructor(readonly code: string) {
    super(code);
  }
}
const bytes = (value: unknown) =>
  new TextEncoder().encode(JSON.stringify(value)).length;
const uuid = (v: unknown): v is string =>
  typeof v === 'string' &&
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(v);
const id = (v: unknown): v is number =>
  Number.isSafeInteger(v) && Number(v) > 0;
const selector = (v: unknown): v is string =>
  typeof v === 'string' &&
  (/^(name|email)$/.test(v) ||
    (/^field:[1-9][0-9]{0,15}$/.test(v) && id(Number(v.slice(6)))));
const object = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === 'object' && !Array.isArray(v);
function keys(v: Record<string, unknown>, allowed: string[]) {
  return Object.keys(v).every((k) => allowed.includes(k));
}

export function isAdminAuthStorageEvent(event: StorageEvent): boolean {
  return (
    event.key === null ||
    event.key === STORAGE_KEYS.ADMIN_PUBKEY ||
    event.key === STORAGE_KEYS.ADMIN_SESSION_TOKEN
  );
}

export function parseLookupRequest(
  value: unknown,
  nonce: string
): LookupRequest {
  if (
    !object(value) ||
    !keys(value, [
      'version',
      'conversation_id',
      'turn_id',
      'call_id',
      'nonce',
      'session_fingerprint',
      'admin_pubkey',
      'expires_at',
      'operation',
      'args',
    ]) ||
    value.version !== 1 ||
    value.nonce !== nonce ||
    !uuid(value.call_id) ||
    !['conversation_id', 'turn_id'].every(
      (k) =>
        typeof value[k] === 'string' &&
        (value[k] as string).length > 0 &&
        (value[k] as string).length <= 100
    ) ||
    !['session_fingerprint', 'admin_pubkey'].every(
      (k) =>
        typeof value[k] === 'string' &&
        /^[0-9a-f]{64}$/.test(value[k] as string)
    ) ||
    typeof value.expires_at !== 'number' ||
    !Number.isSafeInteger(value.expires_at) ||
    value.expires_at > Date.now() + 125000 ||
    !['read_users', 'search_users'].includes(String(value.operation)) ||
    !object(value.args)
  )
    throw new LookupFailure('invalid_request');
  const a = value.args;
  if (
    !keys(a, ['user_ids', 'select', 'where', 'metadata', 'limit', 'cursor']) ||
    !Array.isArray(a.select) ||
    !a.select.length ||
    a.select.length > 3 ||
    !a.select.every(selector) ||
    new Set(a.select).size !== a.select.length ||
    (a.cursor !== undefined && !uuid(a.cursor))
  )
    throw new LookupFailure('invalid_request');
  if (value.operation === 'read_users') {
    if (
      !Array.isArray(a.user_ids) ||
      !a.user_ids.length ||
      a.user_ids.length > 20 ||
      !a.user_ids.every(id) ||
      new Set(a.user_ids).size !== a.user_ids.length ||
      a.where !== undefined ||
      a.metadata !== undefined ||
      a.limit !== undefined
    )
      throw new LookupFailure('invalid_request');
  } else {
    if (
      a.user_ids !== undefined ||
      (a.limit !== undefined && (!id(a.limit) || a.limit > 5)) ||
      !Array.isArray(a.where) ||
      !a.where.length ||
      a.where.length > 3 ||
      !a.where.every(
        (p) =>
          object(p) &&
          keys(p, ['field', 'operator', 'value']) &&
          selector(p.field) &&
          ['equals', 'contains', 'gt', 'gte', 'lt', 'lte'].includes(
            String(p.operator)
          ) &&
          ((typeof p.value === 'string' &&
            Array.from(p.value).length <= 1000 &&
            !/[\uD800-\uDFFF]/u.test(p.value)) ||
            typeof p.value === 'boolean' ||
            (typeof p.value === 'number' && Number.isFinite(p.value)))
      )
    )
      throw new LookupFailure('invalid_request');
    if (
      a.metadata !== undefined &&
      (!object(a.metadata) ||
        !keys(a.metadata, ['approved', 'user_type_id', 'pubkey_present']) ||
        Object.entries(a.metadata).some(
          ([k, v]) =>
            v !== null &&
            (k === 'user_type_id' ? !id(v) : typeof v !== 'boolean')
        ))
    )
      throw new LookupFailure('invalid_request');
  }
  return value as unknown as LookupRequest;
}
function excerpt(value: string): Cell {
  let out = '';
  for (const char of value) {
    if (bytes(out + char) > 80) break;
    out += char;
  }
  return { status: 'ok', value: out, excerpt: out.length < value.length };
}
function matches(value: string, type: string, predicate: Predicate): boolean {
  const expected = predicate.value;
  if (type === 'number') {
    if (typeof expected !== 'number' || predicate.operator === 'contains')
      throw new LookupFailure('invalid_request');
    const number = value.trim() ? Number(value) : NaN;
    if (!Number.isFinite(number)) return false;
    switch (predicate.operator) {
      case 'equals':
        return number === expected;
      case 'gt':
        return number > expected;
      case 'gte':
        return number >= expected;
      case 'lt':
        return number < expected;
      case 'lte':
        return number <= expected;
    }
  }
  if (type === 'checkbox') {
    if (typeof expected !== 'boolean' || predicate.operator !== 'equals')
      throw new LookupFailure('invalid_request');
    return (
      (value === 'true' || value === '1') === expected &&
      ['true', 'false', '1', '0'].includes(value)
    );
  }
  if (
    typeof expected !== 'string' ||
    !['equals', 'contains'].includes(predicate.operator)
  )
    throw new LookupFailure('invalid_request');
  return predicate.operator === 'equals'
    ? value.toLowerCase() === expected.toLowerCase()
    : value.toLowerCase().includes(expected.toLowerCase());
}
function raceAbort<T>(promise: Promise<T>, signal: AbortSignal): Promise<T> {
  if (signal.aborted) return Promise.reject(new LookupFailure('cancelled'));
  return new Promise((resolve, reject) => {
    const abort = () => reject(new LookupFailure('cancelled'));
    signal.addEventListener('abort', abort, { once: true });
    promise
      .then(resolve, reject)
      .finally(() => signal.removeEventListener('abort', abort));
  });
}

export function createUserLookupHandler(options: {
  nonce: string;
  signal: AbortSignal;
  onProgress?: (scanned: number) => void;
}) {
  const lifetime = new AbortController();
  const cursors = new Map<string, ScanState>();
  const cache = new Map<
    string,
    { fingerprint: string; value: string | null }
  >();
  let cacheBytes = 0;
  let binding: string | undefined;
  const dispose = () => {
    lifetime.abort();
    cursors.clear();
    cache.clear();
    cacheBytes = 0;
  };
  options.signal.addEventListener('abort', dispose, { once: true });
  if (options.signal.aborted) dispose();
  const onStorage = (event: StorageEvent) => {
    if (isAdminAuthStorageEvent(event)) dispose();
  };
  window.addEventListener('enclave:admin-auth-cleared', dispose);
  window.addEventListener('storage', onStorage);
  async function handle(raw: unknown): Promise<LookupResult> {
    const request = parseLookupRequest(raw, options.nonce);
    const identity = JSON.stringify([
      request.conversation_id,
      request.turn_id,
      request.admin_pubkey,
      request.session_fingerprint,
    ]);
    if (binding && binding !== identity) {
      dispose();
      return { status: 'error', code: 'session_changed' };
    }
    binding = identity;
    const controller = new AbortController();
    const stop = () => controller.abort();
    lifetime.signal.addEventListener('abort', stop, { once: true });
    if (lifetime.signal.aborted) stop();
    const timer = setTimeout(
      stop,
      Math.max(0, request.expires_at - Date.now())
    );
    const check = () => {
      if (Date.now() >= request.expires_at) throw new LookupFailure('expired');
      if (controller.signal.aborted) throw new LookupFailure('cancelled');
    };
    async function awaitLive<T>(promise: Promise<T>): Promise<T> {
      check();
      const value = await raceAbort(promise, controller.signal);
      check();
      return value;
    }
    let lastSessionCheck = 0;
    let sessionCheck: Promise<void> | undefined;
    let authFailure: LookupFailure | undefined;
    const validateSession = async (force = false) => {
      check();
      if (!force && Date.now() - lastSessionCheck < 1000) return;
      if (!sessionCheck) {
        sessionCheck = (async () => {
          const response = await awaitLive(
            fetch(`${API_BASE}/admin/user-lookup/session`, {
              credentials: 'include',
              cache: 'no-store',
              signal: controller.signal,
            })
          );
          if (!response.ok) throw new LookupFailure('session_changed');
          const session = await awaitLive(response.json());
          if (
            session.pubkey !== request.admin_pubkey ||
            session.session_fingerprint !== request.session_fingerprint
          )
            throw new LookupFailure('session_changed');
          lastSessionCheck = Date.now();
        })().finally(() => {
          sessionCheck = undefined;
        });
      }
      await sessionCheck;
      check();
    };
    const guard = async (forceSession = false) => {
      check();
      if (!hasNip04Support() || !window.nostr?.getPublicKey)
        throw new LookupFailure('no_signer');
      const signer = window.nostr;
      await validateSession(forceSession);
      let key: string;
      try {
        key = await awaitLive(signer.getPublicKey());
      } catch (error) {
        check();
        if (error instanceof LookupFailure) throw error;
        throw new LookupFailure('signer_denied');
      }
      check();
      if (window.nostr !== signer || key !== request.admin_pubkey)
        throw new LookupFailure('key_mismatch');
    };
    // The signer can leave a permission dialog open indefinitely. Refresh only
    // the credential in the background, never initiate another signer prompt.
    const sessionMonitor = setInterval(() => {
      void validateSession(true).catch(() => {
        authFailure = new LookupFailure('session_changed');
        dispose();
      });
    }, 1000);
    const projection = async (
      body: Record<string, unknown>
    ): Promise<Projection> => {
      await guard(true);
      const response = await awaitLive(
        fetch(`${API_BASE}/admin/user-lookup/projection`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'include',
          cache: 'no-store',
          signal: controller.signal,
          body: JSON.stringify(body),
        })
      );
      if (!response.ok)
        throw new LookupFailure(
          response.status === 401
            ? 'session_changed'
            : response.status === 400 || response.status === 422
              ? 'invalid_request'
              : 'projection_failed'
        );
      const data = (await awaitLive(response.json())) as Projection;
      await guard();
      return data;
    };
    const value = async (
      user: Projection['users'][number],
      field: string
    ): Promise<string | null | undefined> => {
      check();
      const encrypted = user.values[field];
      const key = `${request.admin_pubkey}:${user.id}:${field}`;
      if (encrypted === null || encrypted === undefined) {
        const old = cache.get(key);
        if (old) cacheBytes -= bytes(old);
        cache.delete(key);
        return undefined;
      }
      if ('value' in encrypted) {
        const old = cache.get(key);
        if (old) cacheBytes -= bytes(old);
        cache.delete(key);
        return encrypted.value;
      }
      const fingerprint = JSON.stringify(encrypted);
      const previous = cache.get(key);
      if (previous?.fingerprint === fingerprint) return previous.value;
      if (previous) {
        cacheBytes -= bytes(previous);
        cache.delete(key);
      }
      const plaintext = await awaitLive(
        decryptField(encrypted, { signal: controller.signal, check: guard })
      );
      check();
      const entry = { fingerprint, value: plaintext };
      const size = bytes(entry);
      if (size <= 131072) {
        while (cache.size >= 128 || cacheBytes + size > 131072) {
          const first = cache.keys().next().value;
          if (first === undefined) break;
          cacheBytes -= bytes(cache.get(first));
          cache.delete(first);
        }
        cache.set(key, entry);
        cacheBytes += size;
      }
      return plaintext;
    };
    try {
      check();
      await guard();
      const { cursor, ...args } = request.args;
      const key = JSON.stringify([request.operation, args]);
      let state: ScanState;
      if (cursor) {
        const saved = cursors.get(cursor);
        if (!saved || saved.key !== key)
          throw new LookupFailure('invalid_cursor');
        cursors.delete(cursor);
        state = saved;
      } else
        state = {
          key,
          pending: args.user_ids ? [...args.user_ids] : [],
          after: 0,
          coverage: {
            scanned: 0,
            eligible: args.user_ids?.length ?? 0,
            unreadable: 0,
            complete: false,
          },
          matched: 0,
        };
      const isRead = request.operation === 'read_users';
      if (!isRead && !state.pending.length && !state.coverage.complete) {
        const fields = [...new Set(args.where!.map((p) => p.field))];
        let scannedThisCall = 0;
        while (
          scannedThisCall < 1000 &&
          !state.coverage.complete &&
          state.pending.length < (args.limit ?? 5)
        ) {
          const page = await projection({
            select: fields,
            where: args.where,
            metadata: args.metadata ?? {},
            after_id: state.after,
            ...(state.through !== undefined
              ? { through_id: state.through }
              : {}),
            limit: 20,
          });
          state.through = page.through_id;
          state.coverage.eligible = page.eligible;
          const types = new Map(page.fields.map((f) => [f.selector, f.type]));
          for (const user of page.users) {
            check();
            let match = true;
            let unreadable = false;
            for (const predicate of args.where!) {
              const rawValue = await value(user, predicate.field);
              if (rawValue === null) unreadable = true;
              else if (
                rawValue === undefined ||
                !matches(rawValue, types.get(predicate.field) ?? '', predicate)
              )
                match = false;
            }
            state.coverage.scanned++;
            scannedThisCall++;
            if (unreadable) state.coverage.unreadable++;
            else if (match) {
              state.pending.push(user.id);
              state.matched++;
            }
          }
          if (page.next_after_id !== null && page.next_after_id <= state.after)
            throw new LookupFailure('projection_failed');
          state.after = page.next_after_id ?? state.through;
          state.coverage.complete = page.next_after_id === null;
          options.onProgress?.(state.coverage.scanned);
        }
      }
      const users: Row[] = [];
      const continuation = crypto.randomUUID();
      const result = (): Extract<LookupResult, { status: 'ok' }> => ({
        status: 'ok',
        users,
        coverage: { ...state.coverage },
        matched_so_far: state.matched,
        matched_count:
          state.coverage.complete && state.coverage.unreadable === 0
            ? state.matched
            : null,
        next_scan:
          !state.pending.length && !state.coverage.complete
            ? continuation
            : null,
        next_page: state.pending.length ? continuation : null,
        consistency: 'live_id_window',
      });
      if (state.pending.length) {
        const pageIds = state.pending.slice(0, isRead ? 20 : (args.limit ?? 5));
        const page = await projection({
          user_ids: pageIds,
          select: args.select,
        });
        const projected = new Map(page.users.map((u) => [u.id, u]));
        while (state.pending.length && users.length < pageIds.length) {
          const userId = state.pending[0];
          const user = projected.get(userId);
          const row: Row = {
            id: userId,
            status: user ? 'ok' : 'missing_user',
            fields: {},
          };
          let unreadable = false;
          if (user)
            for (const field of args.select) {
              const rawValue = await value(user, field);
              row.fields[field] =
                rawValue === undefined
                  ? { status: 'missing' }
                  : rawValue === null
                    ? { status: 'unreadable' }
                    : excerpt(rawValue);
              if (rawValue === null) unreadable = true;
            }
          users.push(row);
          const previousCoverage = { ...state.coverage };
          const previousMatched = state.matched;
          state.pending.shift();
          if (isRead) {
            state.coverage.scanned++;
            if (user) state.matched++;
            if (unreadable) state.coverage.unreadable++;
            state.coverage.complete = !state.pending.length;
          }
          // Include the candidate's final counters and continuation in its size.
          if (bytes(result()) > 1200) {
            users.pop();
            state.pending.unshift(userId);
            state.coverage = previousCoverage;
            state.matched = previousMatched;
            break;
          }
        }
      }
      await guard(true);
      const packed = result();
      if (bytes(packed) > 1200) throw new LookupFailure('capacity');
      if (packed.next_page || packed.next_scan) {
        if (cursors.size >= 16) throw new LookupFailure('capacity');
        cursors.set(continuation, state);
      }
      return packed;
    } catch (error) {
      const code =
        authFailure?.code ??
        (Date.now() >= request.expires_at
          ? 'expired'
          : error instanceof LookupFailure
            ? error.code
            : controller.signal.aborted
              ? 'cancelled'
              : error instanceof Error && error.message === 'signer_denied'
                ? 'signer_denied'
                : 'projection_failed');
      if (
        ['session_changed', 'key_mismatch', 'cancelled', 'expired'].includes(
          code
        )
      )
        dispose();
      return { status: 'error', code };
    } finally {
      clearTimeout(timer);
      clearInterval(sessionMonitor);
      lifetime.signal.removeEventListener('abort', stop);
    }
  }
  return {
    handle,
    dispose: () => {
      dispose();
      options.signal.removeEventListener('abort', dispose);
      window.removeEventListener('storage', onStorage);
      window.removeEventListener('enclave:admin-auth-cleared', dispose);
    },
  };
}
