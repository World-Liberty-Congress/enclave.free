import { describe, it, expect, vi } from 'vitest';
import { createUserLookupHandler, parseLookupRequest } from './adminUserLookup';

const pubkey = 'a'.repeat(64);
const nonce = '11111111-1111-4111-8111-111111111111';
const event = (args: unknown) => ({
  version: 1,
  nonce,
  conversation_id: 'conversation',
  turn_id: 'turn',
  call_id: crypto.randomUUID(),
  admin_pubkey: pubkey,
  session_fingerprint: 'b'.repeat(64),
  expires_at: Date.now() + 120000,
  operation: 'read_users',
  args,
});

describe('Admin browser user lookup', () => {
  it('reads five requested names beyond the first 25 without decrypting unrelated rich profiles', async () => {
    const decrypt = vi.fn(async (_key: string, cipher: string) => cipher);
    window.nostr = {
      getPublicKey: vi.fn(async () => pubkey),
      nip04: { decrypt },
    } as unknown as NonNullable<Window['nostr']>;
    const ids = [996, 997, 998, 999, 1000];
    const fetcher = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input).endsWith('/session'))
          return Response.json({ pubkey, session_fingerprint: 'b'.repeat(64) });
        const body = JSON.parse(String(init?.body));
        expect(body.user_ids).toEqual(ids);
        expect(body.select).toEqual(['name']);
        return Response.json({
          users: ids.map((id) => ({
            id,
            values: {
              name: { ciphertext: `User ${id}`, ephemeral_pubkey: pubkey },
            },
          })),
          fields: [{ selector: 'name', type: 'text' }],
          through_id: 1000,
          next_after_id: null,
          eligible: 5,
        });
      }
    );
    vi.stubGlobal('fetch', fetcher);
    const lookup = createUserLookupHandler({
      nonce,
      signal: new AbortController().signal,
    });
    const result = await lookup.handle(
      event({ user_ids: ids, select: ['name'] })
    );
    expect(result.status).toBe('ok');
    if (result.status !== 'ok') throw Error('lookup failed');
    expect(result.users.map((u) => u.id)).toEqual(ids);
    expect(decrypt).toHaveBeenCalledTimes(5);
    expect(
      new TextEncoder().encode(JSON.stringify(result)).length
    ).toBeLessThanOrEqual(1200);
    lookup.dispose();
  });
});

function roster(
  size: number,
  values: (id: number, field: string) => string | null = (id, field) =>
    field === 'name' ? `User ${id}` : id === 999 ? 'Nicaragua' : 'Costa Rica'
) {
  const decrypt = vi.fn(async (_key: string, cipher: string) => {
    if (cipher === 'UNREADABLE') throw Error('corrupt cipher');
    return cipher;
  });
  window.nostr = {
    getPublicKey: vi.fn(async () => pubkey),
    nip04: { decrypt },
  } as unknown as NonNullable<Window['nostr']>;
  const projections: Record<string, unknown>[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/session'))
        return Response.json({ pubkey, session_fingerprint: 'b'.repeat(64) });
      const body = JSON.parse(String(init?.body));
      if (body.user_ids?.length > 20) return Response.json({}, { status: 422 });
      projections.push(body);
      const through = body.through_id ?? size;
      const ids: number[] =
        body.user_ids ??
        Array.from(
          {
            length: Math.min(
              body.limit,
              Math.max(0, through - (body.after_id ?? 0))
            ),
          },
          (_, i) => (body.after_id ?? 0) + i + 1
        );
      return Response.json({
        users: ids
          .filter((id) => id <= size)
          .map((id) => ({
            id,
            values: Object.fromEntries(
              body.select.map((field: string) => {
                const v = values(id, field);
                return [
                  field,
                  v === null
                    ? null
                    : { ciphertext: v, ephemeral_pubkey: pubkey },
                ];
              })
            ),
          })),
        fields: body.select.map((selector: string) => ({
          selector,
          type: selector === 'field:4' ? 'number' : 'text',
        })),
        through_id: through,
        next_after_id:
          !body.user_ids && ids[ids.length - 1] !== through
            ? ids[ids.length - 1]
            : null,
        eligible: size,
      });
    })
  );
  return { decrypt, projections };
}
const search = (args: unknown) => ({
  ...event(args),
  operation: 'search_users',
});

it('pages exact reads within the byte budget when coverage counters gain a digit', async () => {
  roster(20, (id) => 'x'.repeat(id === 10 ? 7 : 13));
  const lookup = createUserLookupHandler({
    nonce,
    signal: new AbortController().signal,
  });
  const ids = Array.from({ length: 20 }, (_, index) => index + 1);
  const received: number[] = [];
  let cursor: string | undefined;
  try {
    for (let page = 0; page < 20; page++) {
      const result = await lookup.handle(
        event({
          user_ids: ids,
          select: ['name'],
          ...(cursor ? { cursor } : {}),
        })
      );
      if (result.status !== 'ok') throw Error(result.code);
      expect(
        new TextEncoder().encode(JSON.stringify(result)).length
      ).toBeLessThanOrEqual(1200);
      expect(result.users.length).toBeGreaterThan(0);
      received.push(...result.users.map((user) => user.id));
      expect(result.coverage.scanned).toBe(received.length);
      expect(result.matched_so_far).toBe(received.length);
      expect(result.next_scan).toBeNull();
      if (!result.next_page) {
        expect(result.coverage.complete).toBe(true);
        expect(result.matched_count).toBe(20);
        break;
      }
      expect(result.coverage.complete).toBe(false);
      cursor = result.next_page;
    }
    expect(received).toEqual(ids);
  } finally {
    lookup.dispose();
  }
});

it('finds an encrypted match at ID 999 in one bounded normal tool call', async () => {
  const { decrypt, projections } = roster(1000);
  const progress = vi.fn();
  const lookup = createUserLookupHandler({
    nonce,
    signal: new AbortController().signal,
    onProgress: progress,
  });
  const result = await lookup.handle(
    search({
      select: ['name'],
      where: [{ field: 'field:1', operator: 'equals', value: 'Nicaragua' }],
    })
  );
  expect(result.status).toBe('ok');
  if (result.status !== 'ok') throw Error(result.code);
  expect(result.users.map((u) => u.id)).toEqual([999]);
  expect(result.coverage).toEqual({
    scanned: 1000,
    eligible: 1000,
    unreadable: 0,
    complete: true,
  });
  expect(result.matched_count).toBe(1);
  expect(decrypt).toHaveBeenCalledTimes(1001);
  expect(projections.filter((p) => p.user_ids)).toEqual([
    { user_ids: [999], select: ['name'] },
  ]);
  expect(progress).toHaveBeenCalledWith(1000);
  lookup.dispose();
});

it('drains buffered matches separately from scanning and never drops rich duplicate-name rows', async () => {
  const { projections } = roster(24, (_id, field) =>
    field === 'field:1'
      ? 'yes'
      : field === 'name'
        ? 'Duplicate name'
        : '🌈"\\'.repeat(1000)
  );
  const lookup = createUserLookupHandler({
    nonce,
    signal: new AbortController().signal,
  });
  const args = {
    select: ['name', 'email', 'field:3'],
    where: [{ field: 'field:1', operator: 'equals', value: 'yes' }],
    limit: 5,
  };
  const found: number[] = [];
  let cursor: string | undefined;
  let count = 0;
  let sawPage = false;
  let sawScan = false;
  do {
    const result = await lookup.handle(
      search({ ...args, ...(cursor ? { cursor } : {}) })
    );
    if (result.status !== 'ok') throw Error(result.code);
    expect(bytesForTest(result)).toBeLessThanOrEqual(1200);
    found.push(...result.users.map((u) => u.id));
    result.users.forEach((u) =>
      expect(u.fields.email).toMatchObject({ status: 'ok', excerpt: true })
    );
    if (result.next_page) {
      sawPage = true;
      expect(result.next_scan).toBeNull();
    }
    if (result.next_scan) sawScan = true;
    cursor = result.next_page ?? result.next_scan ?? undefined;
    expect(++count).toBeLessThan(30);
  } while (cursor);
  expect(found).toEqual(Array.from({ length: 24 }, (_, i) => i + 1));
  expect(sawPage && sawScan).toBe(true);
  expect(projections.filter((p) => p.after_id !== undefined)).toHaveLength(2);
  lookup.dispose();
});
const bytesForTest = (value: unknown) =>
  new TextEncoder().encode(JSON.stringify(value)).length;

it('evaluates full values and stable field IDs before excerpting outputs', async () => {
  roster(3, (id, field) =>
    field === 'field:1'
      ? 'Wrong label match'
      : field === 'field:2' && id === 3
        ? 'x'.repeat(10000) + 'NEEDLE'
        : `User ${id}`
  );
  const lookup = createUserLookupHandler({
    nonce,
    signal: new AbortController().signal,
  });
  const result = await lookup.handle(
    search({
      select: ['field:2'],
      where: [{ field: 'field:2', operator: 'contains', value: 'needle' }],
    })
  );
  if (result.status !== 'ok') throw Error(result.code);
  expect(result.users.map((u) => u.id)).toEqual([3]);
  expect(result.users[0].fields['field:2']).toMatchObject({
    excerpt: true,
    value: 'x'.repeat(78),
  });
  lookup.dispose();
});

it('never reports exact zero for unreadable or partially scanned populations', async () => {
  roster(1001, () => 'UNREADABLE');
  const lookup = createUserLookupHandler({
    nonce,
    signal: new AbortController().signal,
  });
  const result = await lookup.handle(
    search({
      select: ['name'],
      where: [{ field: 'name', operator: 'equals', value: 'nobody' }],
    })
  );
  if (result.status !== 'ok') throw Error(result.code);
  expect(result.users).toEqual([]);
  expect(result.coverage).toEqual({
    scanned: 1000,
    eligible: 1001,
    unreadable: 1000,
    complete: false,
  });
  expect(result.matched_count).toBeNull();
  expect(result.next_scan).toEqual(expect.any(String));
  const next = await lookup.handle(
    search({
      select: ['name'],
      where: [{ field: 'name', operator: 'equals', value: 'nobody' }],
      cursor: result.next_scan,
    })
  );
  expect(next).toMatchObject({
    matched_count: null,
    coverage: { complete: true, unreadable: 1001 },
  });
  lookup.dispose();
});

it('distinguishes missing users, missing values and unreadable fields', async () => {
  roster(2, (id) => (id === 1 ? null : 'UNREADABLE'));
  const lookup = createUserLookupHandler({
    nonce,
    signal: new AbortController().signal,
  });
  const result = await lookup.handle(
    event({ user_ids: [1, 2, 3], select: ['name'] })
  );
  if (result.status !== 'ok') throw Error(result.code);
  expect(result.users).toEqual([
    { id: 1, status: 'ok', fields: { name: { status: 'missing' } } },
    { id: 2, status: 'ok', fields: { name: { status: 'unreadable' } } },
    { id: 3, status: 'missing_user', fields: {} },
  ]);
  lookup.dispose();
});

it('rejects signer mismatch and expired work before starting any decryption', async () => {
  const { decrypt } = roster(5);
  vi.mocked(window.nostr!.getPublicKey).mockResolvedValue('c'.repeat(64));
  const lookup = createUserLookupHandler({
    nonce,
    signal: new AbortController().signal,
  });
  expect(
    await lookup.handle(event({ user_ids: [1], select: ['name'] }))
  ).toEqual({ status: 'error', code: 'key_mismatch' });
  expect(decrypt).not.toHaveBeenCalled();
  lookup.dispose();
  const expired = createUserLookupHandler({
    nonce,
    signal: new AbortController().signal,
  });
  expect(
    await expired.handle({
      ...event({ user_ids: [1], select: ['name'] }),
      expires_at: Date.now() - 1,
    })
  ).toEqual({ status: 'error', code: 'expired' });
  expired.dispose();
});

it('discards a late signer reply on abort and schedules no further prompts', async () => {
  const { decrypt } = roster(3);
  let finish!: (v: string) => void;
  decrypt.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      })
  );
  const abort = new AbortController();
  const lookup = createUserLookupHandler({ nonce, signal: abort.signal });
  const pending = lookup.handle(
    event({ user_ids: [1, 2, 3], select: ['name'] })
  );
  await vi.waitFor(() => expect(decrypt).toHaveBeenCalledTimes(1));
  abort.abort();
  expect(await pending).toMatchObject({ status: 'error', code: 'cancelled' });
  finish('late secret');
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(decrypt).toHaveBeenCalledTimes(1);
  lookup.dispose();
});

it('scans 1000 users at 80ms RTT with batch authentication and a one-second monitor', async () => {
  vi.useFakeTimers();
  try {
    roster(1000);
    const immediate = vi.mocked(fetch).getMockImplementation()!;
    const remote = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        await new Promise((resolve) => setTimeout(resolve, 80));
        return immediate(input, init);
      }
    );
    vi.stubGlobal('fetch', remote);
    const lookup = createUserLookupHandler({
      nonce,
      signal: new AbortController().signal,
    });
    let result: Awaited<ReturnType<typeof lookup.handle>> | undefined;
    const task = lookup
      .handle(
        search({
          select: ['name'],
          where: [{ field: 'field:1', operator: 'equals', value: 'Nicaragua' }],
        })
      )
      .then((r) => {
        result = r;
      });
    let elapsed = 0;
    while (!result && elapsed < 30000) {
      await vi.advanceTimersByTimeAsync(1000);
      elapsed += 1000;
    }
    await task;
    expect(result).toMatchObject({
      status: 'ok',
      users: [{ id: 999 }],
      coverage: { scanned: 1000, complete: true },
    });
    expect(remote.mock.calls.length).toBeLessThan(140);
    expect(elapsed).toBeLessThan(15000);
    lookup.dispose();
  } finally {
    vi.useRealTimers();
  }
});

it('detects credential change during a pending signer prompt within the refresh interval', async () => {
  const { decrypt } = roster(1);
  let finish!: (value: string) => void;
  decrypt.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      })
  );
  const lookup = createUserLookupHandler({
    nonce,
    signal: new AbortController().signal,
  });
  const task = lookup.handle(event({ user_ids: [1], select: ['name'] }));
  await vi.waitFor(() => expect(decrypt).toHaveBeenCalledTimes(1));
  vi.mocked(fetch).mockResolvedValue(
    Response.json({ pubkey, session_fingerprint: 'changed' })
  );
  expect(await task).toMatchObject({
    status: 'error',
    code: 'session_changed',
  });
  finish('late');
  await new Promise((resolve) => setTimeout(resolve, 0));
  lookup.dispose();
});

it('invalidates cached plaintext when a fresh projection changes or removes ciphertext', async () => {
  let cipher: string | null = 'first';
  const { decrypt, projections } = roster(1, () => cipher);
  const lookup = createUserLookupHandler({
    nonce,
    signal: new AbortController().signal,
  });
  const read = () => lookup.handle(event({ user_ids: [1], select: ['name'] }));
  await read();
  await read();
  expect(decrypt).toHaveBeenCalledTimes(1);
  cipher = 'updated';
  expect(await read()).toMatchObject({
    users: [{ fields: { name: { value: 'updated' } } }],
  });
  cipher = null;
  expect(await read()).toMatchObject({
    users: [{ fields: { name: { status: 'missing' } } }],
  });
  cipher = 'updated';
  await read();
  expect(decrypt).toHaveBeenCalledTimes(3);
  expect(projections).toHaveLength(5);
  lookup.dispose();
});

it('rejects foreign, changed-argument and consumed cursors without omitting buffered matches', async () => {
  roster(24, () => 'match');
  const lookup = createUserLookupHandler({
    nonce,
    signal: new AbortController().signal,
  });
  const foreign = createUserLookupHandler({
    nonce,
    signal: new AbortController().signal,
  });
  const args = {
    select: ['name'],
    where: [{ field: 'name', operator: 'equals', value: 'match' }],
    limit: 5,
  };
  const first = await lookup.handle(search(args));
  if (first.status !== 'ok' || !first.next_page)
    throw Error('Expected buffered page');
  const continuation = search({ ...args, cursor: first.next_page });
  expect(await foreign.handle(continuation)).toEqual({
    status: 'error',
    code: 'invalid_cursor',
  });
  expect(
    await lookup.handle(
      search({ ...args, select: ['email'], cursor: first.next_page })
    )
  ).toEqual({ status: 'error', code: 'invalid_cursor' });
  const second = await lookup.handle(continuation);
  if (second.status !== 'ok') throw Error(second.code);
  expect(second.users.map((u) => u.id)).toEqual([6, 7, 8, 9, 10]);
  expect(await lookup.handle(continuation)).toEqual({
    status: 'error',
    code: 'invalid_cursor',
  });
  lookup.dispose();
  foreign.dispose();
});

it('compares numeric predicates by field type instead of lexicographic excerpts', async () => {
  roster(3, (id, field) =>
    field === 'field:4' ? String([2, 10, 100][id - 1]) : `User ${id}`
  );
  const lookup = createUserLookupHandler({
    nonce,
    signal: new AbortController().signal,
  });
  const result = await lookup.handle(
    search({
      select: ['name'],
      where: [{ field: 'field:4', operator: 'gte', value: 10 }],
    })
  );
  if (result.status !== 'ok') throw Error(result.code);
  expect(result.users.map((u) => u.id)).toEqual([2, 3]);
  expect(result.matched_count).toBe(2);
  lookup.dispose();
});

it('discards decrypted values after a signer key switch during the await', async () => {
  const { decrypt } = roster(2);
  decrypt.mockImplementationOnce(async () => {
    vi.mocked(window.nostr!.getPublicKey).mockResolvedValue('c'.repeat(64));
    return 'must be discarded';
  });
  const lookup = createUserLookupHandler({
    nonce,
    signal: new AbortController().signal,
  });
  expect(
    await lookup.handle(event({ user_ids: [1, 2], select: ['name'] }))
  ).toEqual({ status: 'error', code: 'key_mismatch' });
  expect(decrypt).toHaveBeenCalledTimes(1);
  lookup.dispose();
});

it('reports a matched user deleted between live scan and output as missing_user', async () => {
  roster(1, () => 'match');
  const original = vi.mocked(fetch).getMockImplementation()!;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.body && JSON.parse(String(init.body)).user_ids)
        return Response.json({
          users: [],
          fields: [{ selector: 'name', type: 'text' }],
          eligible: 1,
          through_id: 1,
          next_after_id: null,
        });
      return original(input, init);
    })
  );
  const lookup = createUserLookupHandler({
    nonce,
    signal: new AbortController().signal,
  });
  expect(
    await lookup.handle(
      search({
        select: ['name'],
        where: [{ field: 'name', operator: 'equals', value: 'match' }],
      })
    )
  ).toMatchObject({ users: [{ id: 1, status: 'missing_user', fields: {} }] });
  lookup.dispose();
});

it('preserves all sparse-then-dense matches across bounded output projections', async () => {
  const { projections } = roster(40, (id, field) =>
    field === 'name' ? `User ${id}` : id <= 4 || id > 20 ? 'yes' : 'no'
  );
  const lookup = createUserLookupHandler({
    nonce,
    signal: new AbortController().signal,
  });
  const args = {
    select: ['name'],
    where: [{ field: 'field:1', operator: 'equals', value: 'yes' }],
    limit: 5,
  };
  const found: number[] = [];
  let cursor: string | undefined;
  do {
    const result = await lookup.handle(
      search({ ...args, ...(cursor ? { cursor } : {}) })
    );
    if (result.status !== 'ok') throw Error(result.code);
    expect(result.users.every((row) => row.status === 'ok')).toBe(true);
    expect(bytesForTest(result)).toBeLessThanOrEqual(1200);
    found.push(...result.users.map((row) => row.id));
    cursor = result.next_page ?? result.next_scan ?? undefined;
    expect(found.length).toBeLessThanOrEqual(24);
  } while (cursor);
  expect(found).toEqual([
    1,
    2,
    3,
    4,
    ...Array.from({ length: 20 }, (_, i) => 21 + i),
  ]);
  expect(
    projections
      .filter((p) => p.user_ids)
      .every((p) => (p.user_ids as number[]).length <= 20)
  ).toBe(true);
  lookup.dispose();
});

it.each(['a', '😀'])(
  'bounds predicate strings by Unicode scalar count for %s',
  (character) => {
    for (const count of [1000, 1001]) {
      const request = search({
        select: ['name'],
        where: [
          { field: 'name', operator: 'equals', value: character.repeat(count) },
        ],
      });
      if (count === 1000)
        expect(() => parseLookupRequest(request, nonce)).not.toThrow();
      else expect(() => parseLookupRequest(request, nonce)).toThrow();
    }
  }
);
