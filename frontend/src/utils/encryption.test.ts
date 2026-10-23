import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { decryptField, decryptFields } from './encryption';

/**
 * These cover the NIP-04 request queue: concurrent callers must produce one
 * extension prompt at a time, never a burst. See the popup-storm fix.
 */
describe('decryptField request queue', () => {
  let inFlight = 0;
  let maxConcurrent = 0;
  let decrypt: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    inFlight = 0;
    maxConcurrent = 0;
    decrypt = vi.fn(async (_pubkey: string, ciphertext: string) => {
      inFlight += 1;
      maxConcurrent = Math.max(maxConcurrent, inFlight);
      // Yield so a parallel implementation would visibly overlap here.
      await new Promise((resolve) => setTimeout(resolve, 1));
      inFlight -= 1;
      return `plain:${ciphertext}`;
    });
    // @ts-expect-error -- minimal NIP-07 shim for the test
    window.nostr = { nip04: { decrypt } };
  });

  afterEach(() => {
    delete (window as { nostr?: unknown }).nostr;
    vi.restoreAllMocks();
  });

  const field = (n: number) => ({
    ciphertext: `c${n}`,
    ephemeral_pubkey: `p${n}`,
  });

  it('never runs two extension requests at once', async () => {
    const results = await Promise.all(
      Array.from({ length: 8 }, (_, i) => decryptField(field(i)))
    );

    expect(maxConcurrent).toBe(1);
    expect(decrypt).toHaveBeenCalledTimes(8);
    expect(results).toEqual(Array.from({ length: 8 }, (_, i) => `plain:c${i}`));
  });

  it('serializes decryptFields too, since it fans out internally', async () => {
    const decrypted = await decryptFields({
      email: field(1),
      name: field(2),
      country: field(3),
    });

    expect(maxConcurrent).toBe(1);
    expect(decrypted).toEqual({
      email: 'plain:c1',
      name: 'plain:c2',
      country: 'plain:c3',
    });
  });

  it('keeps serving later callers after one request rejects', async () => {
    decrypt.mockImplementationOnce(async () => {
      throw new Error('user rejected the approval prompt');
    });

    const [first, second] = await Promise.all([
      decryptField(field(1)),
      decryptField(field(2)),
    ]);

    // A rejection resolves to null and must not stall or poison the queue.
    expect(first).toBeNull();
    expect(second).toBe('plain:c2');
    expect(maxConcurrent).toBe(1);
  });

  it('does not call the extension when there is nothing to decrypt', async () => {
    expect(await decryptField(null)).toBeNull();
    expect(await decryptField(undefined)).toBeNull();
    expect(decrypt).not.toHaveBeenCalled();
  });
});

it('checks canceled work inside the shared queue without poisoning another queue user', async () => {
  let finish!: (value: string) => void;
  const decrypt = vi
    .fn()
    .mockImplementationOnce(
      () =>
        new Promise<string>((resolve) => {
          finish = resolve;
        })
    )
    .mockResolvedValue('unrelated');
  window.nostr = { nip04: { decrypt } } as unknown as NonNullable<
    Window['nostr']
  >;
  const running = decryptField({ ciphertext: 'first', ephemeral_pubkey: 'p' });
  await vi.waitFor(() => expect(decrypt).toHaveBeenCalledTimes(1));
  const controller = new AbortController();
  const queued = decryptField(
    { ciphertext: 'cancelled', ephemeral_pubkey: 'p' },
    { signal: controller.signal, check: async () => {} }
  );
  const rejected = expect(queued).rejects.toBeDefined();
  controller.abort();
  const unrelated = decryptField({
    ciphertext: 'other',
    ephemeral_pubkey: 'p',
  });
  finish('first');
  await running;
  await rejected;
  expect(await unrelated).toBe('unrelated');
  expect(decrypt.mock.calls.map((call) => call[1])).toEqual(['first', 'other']);
  delete window.nostr;
});
