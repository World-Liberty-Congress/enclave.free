import { API_BASE } from '../types/onboarding';
import { decryptField, type EncryptedField } from './encryption';
import {
  createAdminSignerCall,
  isAdminAuthStorageEvent,
  LookupFailure,
} from './adminUserLookup';

type Args = {
  user_ids?: number[];
  start?: string;
  end?: string;
  feedback?: 'any' | 'up' | 'down' | 'none';
  source?: 'user' | 'admin_test' | 'all';
  select?: 'transcript' | 'feedback' | 'both';
  cursor?: string;
};
export type ConversationLookupRequest = {
  version: 1;
  conversation_id: string;
  turn_id: string;
  call_id: string;
  nonce: string;
  session_fingerprint: string;
  admin_pubkey: string;
  expires_at: number;
  operation: 'read_conversations';
  args: Args;
};
type RecordStatus = 'ok' | 'unreadable' | 'missing' | 'changed';
export type ConversationRecord = {
  conversation_id: string;
  message_id: string | null;
  user_id: number | null;
  kind: 'message' | 'feedback' | 'log';
  role: string | null;
  rating: string | null;
  timestamp: string | null;
  legacy_id: boolean;
  status: RecordStatus;
  text: string | null;
  offset: number;
  excerpt: boolean;
  continuation: boolean;
};
type Coverage = {
  candidate_logs: number;
  visited_logs: number;
  unreadable_records: number;
  missing_logs: number;
  changed_logs: number;
  unknown_time_records: number;
  complete: boolean;
  scope: 'retained_review_logs';
  time_basis: 'message_ts';
};
export type ConversationLookupResult =
  | { status: 'error'; code: string }
  | {
      status: 'ok';
      page_type: 'conversation_text';
      records: ConversationRecord[];
      coverage: Coverage;
      next_page: string | null;
      consistency: 'live_id_window';
    };
type Feedback = {
  message_id: string;
  rating: 'up' | 'down';
  comment_ciphertext: string | null;
  comment_ephemeral_pubkey: string | null;
};
type Log = {
  id: number;
  conversation_id: string;
  user_id: number | null;
  encrypted_to_pubkey: string;
  transcript: EncryptedField | null;
  feedback: Feedback[];
  status: 'ok' | 'oversized';
};
type Projection = {
  logs: Log[];
  through_id: number;
  next_after_id: number | null;
  eligible_logs: number;
  time_basis: 'message_ts';
};
type State = {
  key: string;
  after: number;
  through?: number;
  log?: { id: number; idText: string; digest: string; more: boolean };
  index: number;
  offset: number;
  expires: number;
  coverage: Coverage;
};
const object = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === 'object' && !Array.isArray(v);
const keys = (v: Record<string, unknown>, allowed: string[]) =>
  Object.keys(v).every((k) => allowed.includes(k));
const uuid = (v: unknown): v is string =>
  typeof v === 'string' &&
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(v);
const id = (v: unknown): v is number =>
  Number.isSafeInteger(v) && Number(v) > 0;
const messageId = (v: unknown): v is string =>
  typeof v === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(v);
const bytes = (value: unknown) =>
  new TextEncoder().encode(JSON.stringify(value)).length;
const utc = (v: unknown): v is string =>
  typeof v === 'string' &&
  v.length <= 40 &&
  /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(Z|\+00:00)$/.test(v) &&
  Number.isFinite(Date.parse(v)) &&
  new Date(v).toISOString().slice(0, 10) === v.slice(0, 10);

export function parseConversationLookupRequest(
  raw: unknown,
  nonce: string
): ConversationLookupRequest {
  if (
    !object(raw) ||
    !keys(raw, [
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
    raw.version !== 1 ||
    raw.nonce !== nonce ||
    raw.operation !== 'read_conversations' ||
    !uuid(raw.call_id) ||
    !['conversation_id', 'turn_id'].every(
      (k) =>
        typeof raw[k] === 'string' &&
        (raw[k] as string).length > 0 &&
        (raw[k] as string).length <= 100
    ) ||
    !['admin_pubkey', 'session_fingerprint'].every(
      (k) =>
        typeof raw[k] === 'string' && /^[0-9a-f]{64}$/.test(raw[k] as string)
    ) ||
    !Number.isSafeInteger(raw.expires_at) ||
    Number(raw.expires_at) > Date.now() + 125000 ||
    !object(raw.args)
  )
    throw new LookupFailure('invalid_request');
  const a = raw.args;
  if (
    !keys(a, [
      'user_ids',
      'start',
      'end',
      'feedback',
      'source',
      'select',
      'cursor',
    ]) ||
    (a.user_ids !== undefined &&
      (!Array.isArray(a.user_ids) ||
        !a.user_ids.length ||
        a.user_ids.length > 20 ||
        !a.user_ids.every(id) ||
        new Set(a.user_ids).size !== a.user_ids.length)) ||
    (a.start !== undefined && !utc(a.start)) ||
    (a.end !== undefined && !utc(a.end)) ||
    (utc(a.start) && utc(a.end) && Date.parse(a.start) >= Date.parse(a.end)) ||
    (a.feedback !== undefined &&
      !['any', 'up', 'down', 'none'].includes(String(a.feedback))) ||
    (a.source !== undefined &&
      !['user', 'admin_test', 'all'].includes(String(a.source))) ||
    (a.select !== undefined &&
      !['transcript', 'feedback', 'both'].includes(String(a.select))) ||
    (a.cursor !== undefined && !uuid(a.cursor))
  )
    throw new LookupFailure('invalid_request');
  return raw as ConversationLookupRequest;
}

function base(
  log: Pick<Log, 'conversation_id' | 'user_id'>,
  status: RecordStatus = 'ok'
): ConversationRecord {
  return {
    conversation_id: log.conversation_id,
    user_id: log.user_id,
    message_id: null,
    kind: 'log',
    role: null,
    rating: null,
    timestamp: null,
    legacy_id: false,
    status,
    text: null,
    offset: 0,
    excerpt: false,
    continuation: false,
  };
}
async function digest(value: unknown) {
  const hash = await crypto.subtle.digest(
    'SHA-256',
    new TextEncoder().encode(JSON.stringify(value))
  );
  return Array.from(new Uint8Array(hash), (b) =>
    b.toString(16).padStart(2, '0')
  ).join('');
}

export function createConversationLookupHandler(options: {
  nonce: string;
  signal: AbortSignal;
}) {
  const lifetime = new AbortController();
  const cursors = new Map<string, State>();
  let binding: string | undefined;
  const dispose = () => {
    lifetime.abort();
    cursors.clear();
  };
  const storage = (e: StorageEvent) => {
    if (isAdminAuthStorageEvent(e)) dispose();
  };
  options.signal.addEventListener('abort', dispose, { once: true });
  window.addEventListener('storage', storage);
  window.addEventListener('enclave:admin-auth-cleared', dispose);
  if (options.signal.aborted) dispose();

  async function handle(raw: unknown): Promise<ConversationLookupResult> {
    const request = parseConversationLookupRequest(raw, options.nonce);
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
    const call = createAdminSignerCall(
      request,
      lifetime.signal,
      dispose,
      '/admin/conversation-lookup/session'
    );
    const { guard, check, awaitLive } = call;
    try {
      await guard(true);
      const { cursor, ...args } = request.args;
      const key = JSON.stringify(args);
      let state: State;
      if (cursor) {
        const saved = cursors.get(cursor);
        if (!saved || saved.key !== key || saved.expires <= Date.now())
          throw new LookupFailure('invalid_cursor');
        cursors.delete(cursor);
        state = saved;
      } else {
        state = {
          key,
          after: 0,
          index: 0,
          offset: 0,
          expires: request.expires_at,
          coverage: {
            candidate_logs: 0,
            visited_logs: 0,
            unreadable_records: 0,
            missing_logs: 0,
            changed_logs: 0,
            unknown_time_records: 0,
            complete: false,
            scope: 'retained_review_logs',
            time_basis: 'message_ts',
          },
        };
      }
      const project = async (logId?: string): Promise<Projection> => {
        await guard(true);
        const { select: _select, ...filters } = args;
        const response = await awaitLive(
          fetch(`${API_BASE}/admin/conversation-lookup/projection`, {
            method: 'POST',
            credentials: 'include',
            cache: 'no-store',
            signal: call.controller.signal,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              ...filters,
              after_id: state.after,
              through_id: state.through,
              log_id: logId,
            }),
          })
        );
        if (!response.ok)
          throw new LookupFailure(
            response.status === 401 || response.status === 403
              ? 'session_changed'
              : 'projection_failed'
          );
        const data: unknown = await awaitLive(response.json());
        if (
          !object(data) ||
          !Array.isArray(data.logs) ||
          data.logs.length > 1 ||
          !Number.isSafeInteger(data.through_id) ||
          Number(data.through_id) < 0 ||
          !Number.isSafeInteger(data.eligible_logs) ||
          Number(data.eligible_logs) < 0 ||
          (data.next_after_id !== null && !id(data.next_after_id)) ||
          data.time_basis !== 'message_ts' ||
          bytes(data) > 2200000
        )
          throw new LookupFailure('projection_failed');
        for (const log of data.logs) {
          if (
            !object(log) ||
            !id(log.id) ||
            !messageId(log.conversation_id) ||
            (log.user_id !== null && !id(log.user_id)) ||
            !Array.isArray(log.feedback) ||
            !['ok', 'oversized'].includes(String(log.status))
          )
            throw new LookupFailure('projection_failed');
        }
        await guard();
        return data as Projection;
      };
      const projection = await project(state.log?.idText);
      state.through = projection.through_id;
      state.coverage.candidate_logs = projection.eligible_logs;
      let log = projection.logs[0];
      let records: ConversationRecord[] = [];
      let finishedLog = false;
      let more = state.log?.more ?? projection.next_after_id !== null;
      if (!log) {
        if (state.log) {
          records = [
            base(
              { conversation_id: state.log.idText, user_id: null },
              'missing'
            ),
          ];
          state.coverage.missing_logs++;
          finishedLog = true;
        } else state.coverage.complete = true;
      } else {
        const fingerprint = await awaitLive(digest(log));
        if (state.log && state.log.digest !== fingerprint) {
          records = [base(log, 'changed')];
          state.coverage.changed_logs++;
          finishedLog = true;
        } else {
          state.log = {
            id: log.id,
            idText: log.conversation_id,
            digest: fingerprint,
            more,
          };
          let messages: Record<string, unknown>[] = [];
          let unreadable =
            log.status !== 'ok' ||
            log.encrypted_to_pubkey !== request.admin_pubkey;
          if (!unreadable && log.transcript) {
            const plaintext = await awaitLive(
              decryptField(log.transcript, {
                signal: call.controller.signal,
                check: guard,
              })
            );
            try {
              const value: unknown =
                plaintext === null ? null : JSON.parse(plaintext);
              if (
                !object(value) ||
                !Array.isArray(value.turns) ||
                value.turns.length > 20000 ||
                !value.turns.every(object)
              )
                unreadable = true;
              else messages = value.turns;
            } catch {
              unreadable = true;
            }
          } else unreadable = true;
          const feedback = log.feedback.filter(
            (f) =>
              messageId(f.message_id) &&
              ['up', 'down'].includes(f.rating) &&
              (!args.feedback ||
                args.feedback === 'any' ||
                f.rating === args.feedback)
          );
          const ids = new Set(feedback.map((f) => f.message_id));
          const byId = new Map<string, Record<string, unknown>>();
          const selected: {
            record: ConversationRecord;
            feedback?: Feedback;
          }[] = [];
          const inTime = (timestamp: string | null) =>
            timestamp === null ||
            ((!args.start || Date.parse(timestamp) >= Date.parse(args.start)) &&
              (!args.end || Date.parse(timestamp) < Date.parse(args.end)));
          for (let i = 0; i < messages.length; i++) {
            const message = messages[i];
            if (message.message_id != null && !messageId(message.message_id)) {
              unreadable = true;
              break;
            }
            const stable = messageId(message.message_id)
              ? message.message_id
              : `legacy-${i}`;
            if (byId.has(stable)) {
              unreadable = true;
              break;
            }
            byId.set(stable, message);
            const timestamp = utc(message.ts) ? message.ts : null;
            if (
              args.select === 'feedback' ||
              (args.feedback && args.feedback !== 'none' && !ids.has(stable)) ||
              !inTime(timestamp)
            )
              continue;
            const record = {
              ...base(log),
              kind: 'message' as const,
              message_id: stable,
              role: ['user', 'assistant'].includes(String(message.role))
                ? String(message.role)
                : null,
              timestamp,
              legacy_id: stable.startsWith('legacy-'),
              text:
                typeof message.content === 'string' ? message.content : null,
            };
            if (record.text === null || record.role === null)
              record.status = 'unreadable';
            selected.push({ record });
          }
          if (
            unreadable &&
            (args.select !== 'feedback' ||
              log.encrypted_to_pubkey !== request.admin_pubkey ||
              log.status !== 'ok')
          )
            selected.splice(0, selected.length, {
              record: base(log, 'unreadable'),
            });
          if (
            args.select !== 'transcript' &&
            log.encrypted_to_pubkey === request.admin_pubkey
          ) {
            for (const f of feedback) {
              const timestamp = byId.get(f.message_id)?.ts;
              const ts = utc(timestamp) ? timestamp : null;
              if (inTime(ts))
                selected.push({
                  record: {
                    ...base(log),
                    kind: 'feedback',
                    message_id: f.message_id,
                    rating: f.rating,
                    timestamp: ts,
                    legacy_id: f.message_id.startsWith('legacy-'),
                  },
                  feedback: f,
                });
            }
          }
          const entry = selected[state.index];
          if (entry) {
            const record = entry.record;
            if (entry.feedback?.comment_ciphertext) {
              record.text = await awaitLive(
                decryptField(
                  {
                    ciphertext: entry.feedback.comment_ciphertext,
                    ephemeral_pubkey:
                      entry.feedback.comment_ephemeral_pubkey ?? '',
                  },
                  { signal: call.controller.signal, check: guard }
                )
              );
              if (record.text === null) record.status = 'unreadable';
            }
            if (state.offset === 0) {
              if (record.status === 'unreadable')
                state.coverage.unreadable_records++;
              if (record.kind !== 'log' && record.timestamp === null)
                state.coverage.unknown_time_records++;
            }
            const full = record.text;
            record.offset = state.offset;
            if (full !== null) {
              let text = '';
              // Account for JSON escaping as well as multibyte UTF-8. Reserve the
              // largest possible continuation envelope; never clip serialized JSON.
              for (const character of full.slice(state.offset)) {
                const candidate = {
                  ...record,
                  text: text + character,
                  excerpt: true,
                  continuation: true,
                };
                const trial = {
                  status: 'ok',
                  page_type: 'conversation_text',
                  records: [candidate],
                  coverage: state.coverage,
                  next_page: '00000000-0000-4000-8000-000000000000',
                  consistency: 'live_id_window',
                };
                if (bytes(trial) > 1190) break;
                text += character;
              }
              if (!text && full.length > state.offset)
                throw new LookupFailure('capacity');
              state.offset += text.length;
              record.text = text;
              record.continuation = state.offset < full.length;
              record.excerpt = record.offset > 0 || record.continuation;
            }
            records = [record];
            if (!record.continuation) {
              state.index++;
              state.offset = 0;
            }
            finishedLog = state.index >= selected.length;
          } else finishedLog = true;
          // Revalidate retention/version after potentially long signer prompts.
          const current = (await project(log.conversation_id)).logs[0];
          if (!current || (await awaitLive(digest(current))) !== fingerprint) {
            records = [base(log, current ? 'changed' : 'missing')];
            if (current) state.coverage.changed_logs++;
            else state.coverage.missing_logs++;
            finishedLog = true;
          }
        }
      }
      if (finishedLog && state.log) {
        state.after = state.log.id;
        state.log = undefined;
        state.index = 0;
        state.offset = 0;
        state.coverage.visited_logs++;
        state.coverage.complete = !more;
      }
      await guard(true);
      check();
      const next = state.coverage.complete ? null : crypto.randomUUID();
      const result: ConversationLookupResult = {
        status: 'ok',
        page_type: 'conversation_text',
        records,
        coverage: { ...state.coverage },
        next_page: next,
        consistency: 'live_id_window',
      };
      if (bytes(result) > 1200) throw new LookupFailure('capacity');
      if (next) {
        for (const [token, old] of cursors)
          if (old.expires <= Date.now()) cursors.delete(token);
        if (cursors.size >= 64) throw new LookupFailure('capacity');
        state.expires = request.expires_at;
        cursors.set(next, state);
      }
      return result;
    } catch (error) {
      const code = call.errorCode(error);
      if (
        ['session_changed', 'key_mismatch', 'cancelled', 'expired'].includes(
          code
        )
      )
        dispose();
      return { status: 'error', code };
    } finally {
      call.finish();
    }
  }
  return {
    handle,
    dispose: () => {
      dispose();
      options.signal.removeEventListener('abort', dispose);
      window.removeEventListener('storage', storage);
      window.removeEventListener('enclave:admin-auth-cleared', dispose);
    },
  };
}
