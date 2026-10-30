import { describe, expect, it, vi } from 'vitest';
import { authRequestError } from './authRequestError';

describe('authRequestError', () => {
  it('keeps HTML gateway errors out of the user message', async () => {
    const error = await authRequestError(
      new Response('<html>proxy error</html>', { status: 429 }),
      'Safe fallback'
    );
    expect(error.message).toBe('Safe fallback');
    expect(error.status).toBe(429);
    expect(error.retryAfterSeconds).toBe(60);
  });

  it('preserves a readable JSON error and its retry interval', async () => {
    const error = await authRequestError(
      Response.json(
        { detail: 'Service unavailable' },
        { status: 429, headers: { 'Retry-After': '12' } }
      ),
      'Fallback'
    );
    expect(error.message).toBe('Service unavailable');
    expect(error.retryAfterSeconds).toBe(12);
  });

  it('supports HTTP-date retry timing and malformed headers', async () => {
    const clock = vi
      .spyOn(Date, 'now')
      .mockReturnValue(Date.parse('2026-10-29T12:00:00Z'));
    try {
      const dated = await authRequestError(
        new Response('', {
          status: 429,
          headers: { 'Retry-After': 'Thu, 29 Oct 2026 12:00:15 GMT' },
        }),
        'Fallback'
      );
      expect(dated.retryAfterSeconds).toBe(15);
      const malformed = await authRequestError(
        new Response('invalid json', {
          status: 429,
          headers: {
            'Content-Type': 'application/json',
            'Retry-After': 'invalid',
          },
        }),
        'Fallback'
      );
      expect(malformed.retryAfterSeconds).toBe(60);
      expect(malformed.message).toBe('Fallback');
    } finally {
      clock.mockRestore();
    }
  });
});
