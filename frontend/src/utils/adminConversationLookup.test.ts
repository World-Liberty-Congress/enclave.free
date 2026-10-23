import { afterEach, describe, expect, it, vi } from 'vitest';
import { getPublicKey, nip04 } from 'nostr-tools';
import { createConversationLookupHandler } from './adminConversationLookup';

const adminKey = new Uint8Array(32).fill(1);
const senderKey = new Uint8Array(32).fill(2);
const pubkey = getPublicKey(adminKey);
const nonce = '11111111-1111-4111-8111-111111111111';
const event = (args: Record<string, unknown> = {}) => ({
  version: 1,
  nonce,
  conversation_id: 'admin-conversation',
  turn_id: 'turn',
  call_id: crypto.randomUUID(),
  admin_pubkey: pubkey,
  session_fingerprint: 'b'.repeat(64),
  expires_at: Date.now() + 120000,
  operation: 'read_conversations',
  args,
});
const handlers: ReturnType<typeof createConversationLookupHandler>[] = [];
function handler() {
  const result = createConversationLookupHandler({
    nonce,
    signal: new AbortController().signal,
  });
  handlers.push(result);
  return result;
}
async function fixture(text = 'A private retained message.') {
  const transcript = await nip04.encrypt(
    senderKey,
    pubkey,
    JSON.stringify({
      turns: [{ message_id: 'message-1', role: 'user', content: text }],
    })
  );
  const comment = await nip04.encrypt(
    senderKey,
    pubkey,
    'Helpful private feedback.'
  );
  window.nostr = {
    getPublicKey: async () => pubkey,
    nip04: {
      decrypt: async (key: string, ciphertext: string) =>
        nip04.decrypt(adminKey, key, ciphertext),
    },
  } as NonNullable<Window['nostr']>;
  const projection = {
    logs: [
      {
        id: 1,
        conversation_id: 'review-1',
        user_id: 5,
        source: 'user',
        created_at: '2026-10-20 10:00:00',
        encrypted_to_pubkey: pubkey,
        status: 'ok',
        transcript: {
          ciphertext: transcript,
          ephemeral_pubkey: getPublicKey(senderKey),
        },
        feedback: [
          {
            message_id: 'message-1',
            rating: 'up',
            comment_ciphertext: comment,
            comment_ephemeral_pubkey: getPublicKey(senderKey),
            created_at: '2026-10-20 11:00:00',
          },
        ],
      },
    ],
    through_id: 1,
    next_after_id: null,
    eligible_logs: 1,
    time_basis: 'message_ts',
    consistency: 'live_id_window',
  };
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: RequestInfo | URL) =>
      Response.json(
        String(url).endsWith('/session')
          ? { pubkey, session_fingerprint: 'b'.repeat(64) }
          : projection
      )
    )
  );
  return projection;
}
afterEach(() => {
  handlers.splice(0).forEach((h) => h.dispose());
  vi.unstubAllGlobals();
});

describe('Admin Conversation review browser interface', () => {
  it('decrypts actual NIP-04 transcript and feedback with stable citations and explicit unknown message time', async () => {
    await fixture();
    const lookup = handler();
    const first = await lookup.handle(event());
    expect(first.status).toBe('ok');
    if (first.status !== 'ok') throw new Error(first.code);
    expect(first.records[0]).toMatchObject({
      conversation_id: 'review-1',
      message_id: 'message-1',
      text: 'A private retained message.',
      timestamp: null,
      legacy_id: false,
      kind: 'message',
    });
    expect(first.coverage.complete).toBe(false);
    const second = await lookup.handle(event({ cursor: first.next_page }));
    expect(second.status).toBe('ok');
    if (second.status !== 'ok') throw new Error(second.code);
    expect(second.records[0]).toMatchObject({
      message_id: 'message-1',
      kind: 'feedback',
      rating: 'up',
      text: 'Helpful private feedback.',
    });
    expect(second.coverage).toMatchObject({
      candidate_logs: 1,
      visited_logs: 1,
      complete: true,
      time_basis: 'message_ts',
    });
    expect(second.next_page).toBeNull();
  });
});

it('continues every UTF-8 text chunk within 1200 bytes and rejects replayed/foreign cursors', async () => {
  const full = '“é”🙂\\\n'.repeat(250);
  await fixture(full);
  const lookup = handler();
  const args = { select: 'transcript' };
  let page = await lookup.handle(event(args));
  let collected = '';
  let cursor: string | null = null;
  for (let n = 0; n < 100; n++) {
    if (page.status !== 'ok') throw new Error(page.code);
    expect(
      new TextEncoder().encode(JSON.stringify(page)).length
    ).toBeLessThanOrEqual(1200);
    expect(page.records[0]).toMatchObject({
      conversation_id: 'review-1',
      message_id: 'message-1',
      offset: collected.length,
    });
    collected += page.records[0].text;
    cursor = page.next_page;
    if (!cursor) break;
    expect(page.records[0].excerpt).toBe(true);
    expect(await lookup.handle(event({ select: 'feedback', cursor }))).toEqual({
      status: 'error',
      code: 'invalid_cursor',
    });
    page = await lookup.handle(event({ ...args, cursor }));
    expect(await lookup.handle(event({ ...args, cursor }))).toEqual({
      status: 'error',
      code: 'invalid_cursor',
    });
  }
  expect(cursor).toBeNull();
  expect(collected).toBe(full);
});

it('filters decrypted message times and directly selects negatively rated messages/comments', async () => {
  const projection = await fixture();
  projection.logs[0].transcript.ciphertext = await nip04.encrypt(
    senderKey,
    pubkey,
    JSON.stringify({
      turns: [
        {
          message_id: 'old',
          role: 'user',
          content: 'OUTSIDE INTERVAL',
          ts: '2026-10-01T00:00:00Z',
        },
        {
          message_id: 'message-1',
          role: 'assistant',
          content: 'RECENT RATED ANSWER',
          ts: '2026-10-20T10:00:00Z',
        },
        {
          message_id: 'positive',
          role: 'assistant',
          content: 'POSITIVE ANSWER',
          ts: '2026-10-20T11:00:00Z',
        },
        { message_id: 'unknown', role: 'user', content: 'UNKNOWN TIME' },
      ],
    })
  );
  projection.logs[0].feedback[0].rating = 'down';
  const lookup = handler();
  const args = {
    feedback: 'down',
    start: '2026-10-20T00:00:00Z',
    end: '2026-10-21T00:00:00Z',
  };
  const first = await lookup.handle(event(args));
  if (first.status !== 'ok') throw new Error(first.code);
  expect(first.records[0]).toMatchObject({
    message_id: 'message-1',
    text: 'RECENT RATED ANSWER',
    timestamp: '2026-10-20T10:00:00Z',
  });
  const second = await lookup.handle(
    event({ ...args, cursor: first.next_page })
  );
  if (second.status !== 'ok') throw new Error(second.code);
  expect(second.records[0]).toMatchObject({
    kind: 'feedback',
    rating: 'down',
    text: 'Helpful private feedback.',
  });
  expect(second.coverage.complete).toBe(true);
  const independent = await handler().handle(
    event({ select: 'transcript', start: '2026-10-21T00:00:00Z' })
  );
  if (independent.status !== 'ok') throw new Error(independent.code);
  expect(independent.records[0]).toMatchObject({
    message_id: 'unknown',
    timestamp: null,
    text: 'UNKNOWN TIME',
  });
  expect(independent.coverage.unknown_time_records).toBe(1);
});

it('reports legacy positional IDs, unreadable ciphertext, and empty matching intervals explicitly', async () => {
  const projection = await fixture();
  projection.logs[0].transcript.ciphertext = await nip04.encrypt(
    senderKey,
    pubkey,
    JSON.stringify({ turns: [{ role: 'user', content: 'Legacy text' }] })
  );
  let result = await handler().handle(event({ select: 'transcript' }));
  if (result.status !== 'ok') throw new Error(result.code);
  expect(result.records[0]).toMatchObject({
    message_id: 'legacy-0',
    legacy_id: true,
    timestamp: null,
  });
  projection.logs[0].transcript.ciphertext = 'invalid NIP-04';
  result = await handler().handle(event({ select: 'transcript' }));
  if (result.status !== 'ok') throw new Error(result.code);
  expect(result.records[0]).toMatchObject({ status: 'unreadable', text: null });
  expect(result.coverage.unreadable_records).toBe(1);
  projection.logs[0].transcript.ciphertext = await nip04.encrypt(
    senderKey,
    pubkey,
    JSON.stringify({
      turns: [
        {
          message_id: 'old',
          role: 'user',
          content: 'Outside',
          ts: '2020-01-01T00:00:00Z',
        },
      ],
    })
  );
  result = await handler().handle(event({ start: '2026-01-01T00:00:00Z' }));
  if (result.status !== 'ok') throw new Error(result.code);
  // Existing detached feedback has unknown message time and remains explicit.
  expect(result.records[0].kind).toBe('feedback');
  expect(result.coverage.unknown_time_records).toBe(1);
});

it('does not return decrypted data deleted while the signer dialog was open', async () => {
  const projection = await fixture();
  const original = window.nostr!.nip04!.decrypt;
  window.nostr!.nip04!.decrypt = async (key, ciphertext) => {
    const text = await original(key, ciphertext);
    projection.logs.splice(0);
    return text;
  };
  const result = await handler().handle(event());
  if (result.status !== 'ok') throw new Error(result.code);
  expect(result.records[0]).toMatchObject({ status: 'missing', text: null });
  expect(JSON.stringify(result)).not.toContain('A private retained message');
  expect(result.coverage.missing_logs).toBe(1);
});

it('re-fetches each continuation and reports changed ciphertext instead of mixing versions', async () => {
  const projection = await fixture('long'.repeat(800));
  const lookup = handler();
  const first = await lookup.handle(event({ select: 'transcript' }));
  if (first.status !== 'ok') throw new Error(first.code);
  projection.logs[0].transcript.ciphertext = await nip04.encrypt(
    senderKey,
    pubkey,
    '{"turns":[]}'
  );
  const next = await lookup.handle(
    event({ select: 'transcript', cursor: first.next_page })
  );
  if (next.status !== 'ok') throw new Error(next.code);
  expect(next.records[0]).toMatchObject({ status: 'changed', text: null });
  expect(next.coverage.changed_logs).toBe(1);
});

it('rejects expired, cross-turn and signer-denied calls without delivering transcript text', async () => {
  await fixture();
  expect(
    await handler().handle({ ...event(), expires_at: Date.now() - 1 })
  ).toEqual({ status: 'error', code: 'expired' });
  window.nostr!.nip04!.decrypt = async () => {
    throw new Error('Permission denied');
  };
  expect(await handler().handle(event())).toEqual({
    status: 'error',
    code: 'signer_denied',
  });
  await fixture();
  const lookup = handler();
  const first = await lookup.handle(event());
  if (first.status !== 'ok') throw new Error(first.code);
  expect(
    await lookup.handle({
      ...event({ cursor: first.next_page }),
      turn_id: 'foreign',
    })
  ).toEqual({ status: 'error', code: 'session_changed' });
});

it('cancels a pending signer prompt on abort or credential changes', async () => {
  await fixture();
  const abort = new AbortController();
  let entered = false;
  let resolveSigner!: (text: string) => void;
  window.nostr!.nip04!.decrypt = async () => {
    entered = true;
    return new Promise<string>((resolve) => {
      resolveSigner = resolve;
    });
  };
  const lookup = createConversationLookupHandler({
    nonce,
    signal: abort.signal,
  });
  handlers.push(lookup);
  const pending = lookup.handle(event());
  await vi.waitFor(() => expect(entered).toBe(true));
  abort.abort();
  expect(await pending).toEqual({ status: 'error', code: 'cancelled' });
  resolveSigner('discarded after cancellation');
  await Promise.resolve();
  await fixture();
  const changed = handler();
  window.dispatchEvent(
    new StorageEvent('storage', { key: 'admin_session_token' })
  );
  // Use the app's dedicated auth-clear event, which also clears cookie sessions.
  window.dispatchEvent(new Event('enclave:admin-auth-cleared'));
  expect(await changed.handle(event())).toEqual({
    status: 'error',
    code: 'cancelled',
  });
});

it('preserves the retained store message-ID grammar instead of inventing legacy citations', async () => {
  const projection = await fixture();
  projection.logs[0].transcript.ciphertext = await nip04.encrypt(
    senderKey,
    pubkey,
    JSON.stringify({
      turns: [
        {
          message_id: 'sage:message.42',
          role: 'assistant',
          content: 'Stable citation',
          ts: '2026-10-20T10:00:00Z',
        },
      ],
    })
  );
  const result = await handler().handle(event({ select: 'transcript' }));
  if (result.status !== 'ok') throw new Error(result.code);
  expect(result.records[0]).toMatchObject({
    message_id: 'sage:message.42',
    legacy_id: false,
  });
});

it('reports unreadable feedback projections encrypted for another Admin instead of claiming empty coverage', async () => {
  const projection = await fixture();
  projection.logs[0].encrypted_to_pubkey = 'f'.repeat(64);
  const result = await handler().handle(event({ select: 'feedback' }));
  if (result.status !== 'ok') throw new Error(result.code);
  expect(result.records[0]).toMatchObject({ status: 'unreadable', text: null });
  expect(result.coverage.unreadable_records).toBe(1);
});
