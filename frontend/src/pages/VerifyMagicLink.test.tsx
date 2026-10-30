import { StrictMode } from 'react';
import userEvent from '@testing-library/user-event';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { STORAGE_KEYS } from '../types/onboarding';
import i18n from '../i18n';
import { VerifyMagicLink } from './VerifyMagicLink';

describe('VerifyMagicLink', () => {
  it.each([429, 503])(
    'ignores language changes after HTTP %s until an explicit retry',
    async (status) => {
      const previousLanguage = i18n.language;
      const user = userEvent.setup();
      const fetchMock = vi.fn(
        (_input: RequestInfo | URL, _init?: RequestInit) =>
          Promise.resolve(
            Response.json(
              { detail: 'Temporary failure' },
              { status, headers: { 'Retry-After': '1' } }
            )
          )
      );
      vi.stubGlobal('fetch', fetchMock);
      try {
        render(
          <StrictMode>
            <MemoryRouter initialEntries={['/verify?token=language-token']}>
              <VerifyMagicLink />
            </MemoryRouter>
          </StrictMode>
        );
        await screen.findByText(
          i18n.t(
            status === 429
              ? 'errors.authRateLimited'
              : 'errors.verificationFailed'
          )
        );
        await act(async () => {
          await i18n.changeLanguage('es');
        });
        expect(fetchMock).toHaveBeenCalledTimes(1);
        await act(async () => {
          await i18n.changeLanguage('en');
        });
        await waitFor(
          () =>
            expect(
              screen.getByRole('button', { name: 'Try again' })
            ).toBeEnabled(),
          { timeout: 3000 }
        );
        expect(fetchMock).toHaveBeenCalledTimes(1);
        await user.click(screen.getByRole('button', { name: 'Try again' }));
        await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
        expect(
          fetchMock.mock.calls.map(([, init]) => JSON.parse(String(init?.body)))
        ).toEqual([{ token: 'language-token' }, { token: 'language-token' }]);
      } finally {
        await act(async () => {
          await i18n.changeLanguage(previousLanguage);
        });
      }
    }
  );
  it('preserves a rate-limited token and waits for a manual retry', async () => {
    const user = userEvent.setup();
    let attempts = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/auth/verify') && init?.method === 'POST') {
        attempts += 1;
        return Promise.resolve(
          attempts === 1
            ? new Response('<html>Too many requests</html>', {
                status: 429,
                headers: { 'Retry-After': '1' },
              })
            : Response.json({
                user: {
                  email: 'retry@example.test',
                  approved: true,
                  needs_onboarding: false,
                  needs_user_type: false,
                },
              })
        );
      }
      return Promise.resolve(Response.json({}));
    });
    vi.stubGlobal('fetch', fetchMock);
    render(
      <MemoryRouter initialEntries={['/verify?token=preserved-token']}>
        <VerifyMagicLink />
      </MemoryRouter>
    );
    expect(
      await screen.findByText(
        'Too many sign-in attempts. Please wait before trying again.'
      )
    ).toBeInTheDocument();
    expect(
      screen.queryByText('Link expired or invalid')
    ).not.toBeInTheDocument();
    expect(screen.queryByText('Request a new link')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Try again in/ })).toBeDisabled();
    await waitFor(
      () =>
        expect(screen.getByRole('button', { name: 'Try again' })).toBeEnabled(),
      { timeout: 3000 }
    );
    expect(attempts).toBe(1);
    await user.click(screen.getByRole('button', { name: 'Try again' }));
    expect(await screen.findByText('retry@example.test')).toBeInTheDocument();
    const requests = fetchMock.mock.calls.filter(([input]) =>
      String(input).endsWith('/auth/verify')
    );
    expect(requests).toHaveLength(2);
    expect(requests.map(([, init]) => JSON.parse(String(init?.body)))).toEqual([
      { token: 'preserved-token' },
      { token: 'preserved-token' },
    ]);
  });

  it('offers the same link again after a temporary service failure', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          new Response('<html>Unavailable</html>', { status: 503 })
        )
      )
    );
    render(
      <MemoryRouter initialEntries={['/verify?token=retryable-token']}>
        <VerifyMagicLink />
      </MemoryRouter>
    );
    expect(
      await screen.findByRole('button', { name: 'Try again' })
    ).toBeEnabled();
    expect(
      screen.queryByText('Link expired or invalid')
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/<html>/)).not.toBeInTheDocument();
  });

  it('offers a new link when the server confirms the token is invalid', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          Response.json({ detail: 'Invalid token' }, { status: 401 })
        )
      )
    );
    render(
      <MemoryRouter initialEntries={['/verify?token=invalid-token']}>
        <VerifyMagicLink />
      </MemoryRouter>
    );
    expect(
      await screen.findByText('Link expired or invalid')
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Request a new link' })
    ).toBeEnabled();
  });
  beforeEach(() => {
    const storage = new Map<string, string>();
    vi.stubGlobal('localStorage', {
      getItem: vi.fn((key: string) => storage.get(key) ?? null),
      removeItem: vi.fn((key: string) => {
        storage.delete(key);
      }),
      setItem: vi.fn((key: string, value: string) => {
        storage.set(key, value);
      }),
    });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('verifies a token only once when React StrictMode remounts the page', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/auth/verify') && init?.method === 'POST') {
        return Promise.resolve(
          Response.json({
            success: true,
            user: {
              id: 13,
              email: 'pending-smoke@example.test',
              name: 'Pending Smoke User',
              user_type_id: null,
              approved: false,
              created_at: '2026-06-10 14:53:00',
              needs_onboarding: false,
              needs_user_type: false,
            },
            session_token: 'session-token',
          })
        );
      }

      return Promise.resolve(Response.json({}));
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <StrictMode>
        <MemoryRouter initialEntries={['/verify?token=magic-token']}>
          <Routes>
            <Route path="/verify" element={<VerifyMagicLink />} />
            <Route path="/pending" element={<div>Pending approval</div>} />
          </Routes>
        </MemoryRouter>
      </StrictMode>
    );

    expect(
      await screen.findByText('pending-smoke@example.test')
    ).toBeInTheDocument();

    await waitFor(() => {
      const verifyRequests = fetchMock.mock.calls.filter(([input, init]) => {
        return (
          String(input).endsWith('/auth/verify') && init?.method === 'POST'
        );
      });
      expect(verifyRequests).toHaveLength(1);
    });

    expect(localStorage.getItem(STORAGE_KEYS.USER_APPROVED)).toBe('false');
  });

  it('clears stale selected user type when verified user has no type', async () => {
    localStorage.setItem(STORAGE_KEYS.USER_TYPE_ID, '4');

    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/auth/verify') && init?.method === 'POST') {
        return Promise.resolve(
          Response.json({
            success: true,
            user: {
              id: 15,
              email: 'new-user@example.test',
              name: 'New User',
              user_type_id: null,
              approved: true,
              created_at: '2026-06-10 15:01:00',
              needs_onboarding: true,
              needs_user_type: false,
            },
            session_token: 'session-token',
          })
        );
      }

      if (url.endsWith('/user-types')) {
        return Promise.resolve(Response.json({ types: [] }));
      }

      return Promise.resolve(Response.json({}));
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <MemoryRouter initialEntries={['/verify?token=magic-token']}>
        <Routes>
          <Route path="/verify" element={<VerifyMagicLink />} />
          <Route path="/profile" element={<div>Profile completion</div>} />
        </Routes>
      </MemoryRouter>
    );

    expect(
      await screen.findByText('new-user@example.test')
    ).toBeInTheDocument();

    await waitFor(() => {
      expect(localStorage.getItem(STORAGE_KEYS.USER_TYPE_ID)).toBeNull();
    });
  });

  it('routes unapproved users to profile completion before pending approval when onboarding is incomplete', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/auth/verify') && init?.method === 'POST') {
        return Promise.resolve(
          Response.json({
            success: true,
            user: {
              id: 18,
              email: 'pending-profile@example.test',
              name: 'Pending Profile',
              user_type_id: null,
              approved: false,
              created_at: '2026-07-03 17:30:00',
              needs_onboarding: true,
              needs_user_type: false,
            },
            session_token: 'session-token',
          })
        );
      }

      if (url.endsWith('/user-types')) {
        return Promise.resolve(Response.json({ types: [] }));
      }

      return Promise.resolve(Response.json({}));
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <MemoryRouter initialEntries={['/verify?token=magic-token']}>
        <Routes>
          <Route path="/verify" element={<VerifyMagicLink />} />
          <Route path="/profile" element={<div>Profile completion</div>} />
          <Route path="/pending" element={<div>Pending approval</div>} />
        </Routes>
      </MemoryRouter>
    );

    expect(
      await screen.findByText('pending-profile@example.test')
    ).toBeInTheDocument();

    expect(
      await screen.findByText('Profile completion', {}, { timeout: 4000 })
    ).toBeInTheDocument();
    expect(screen.queryByText('Pending approval')).not.toBeInTheDocument();
    expect(localStorage.getItem(STORAGE_KEYS.USER_APPROVED)).toBe('false');
  });

  it('routes unapproved users to user type selection before pending approval when a type is required', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/auth/verify') && init?.method === 'POST') {
        return Promise.resolve(
          Response.json({
            success: true,
            user: {
              id: 19,
              email: 'pending-type@example.test',
              name: 'Pending Type',
              user_type_id: null,
              approved: false,
              created_at: '2026-07-03 17:31:00',
              needs_onboarding: false,
              needs_user_type: true,
            },
            session_token: 'session-token',
          })
        );
      }

      return Promise.resolve(Response.json({}));
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <MemoryRouter initialEntries={['/verify?token=magic-token']}>
        <Routes>
          <Route path="/verify" element={<VerifyMagicLink />} />
          <Route path="/user-type" element={<div>User type selection</div>} />
          <Route path="/pending" element={<div>Pending approval</div>} />
        </Routes>
      </MemoryRouter>
    );

    expect(
      await screen.findByText('pending-type@example.test')
    ).toBeInTheDocument();

    expect(
      await screen.findByText('User type selection', {}, { timeout: 4000 })
    ).toBeInTheDocument();
    expect(screen.queryByText('Pending approval')).not.toBeInTheDocument();
    expect(localStorage.getItem(STORAGE_KEYS.USER_APPROVED)).toBe('false');
  });

  it('clears stale user name when verified user has no name', async () => {
    localStorage.setItem(STORAGE_KEYS.USER_NAME, 'Previous User');

    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/auth/verify') && init?.method === 'POST') {
        return Promise.resolve(
          Response.json({
            success: true,
            user: {
              id: 16,
              email: 'nameless@example.test',
              name: null,
              user_type_id: null,
              approved: true,
              created_at: '2026-06-10 15:03:00',
              needs_onboarding: false,
              needs_user_type: false,
            },
            session_token: 'session-token',
          })
        );
      }

      return Promise.resolve(Response.json({}));
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <MemoryRouter initialEntries={['/verify?token=magic-token']}>
        <Routes>
          <Route path="/verify" element={<VerifyMagicLink />} />
          <Route path="/chat" element={<div>Chat</div>} />
        </Routes>
      </MemoryRouter>
    );

    expect(
      await screen.findByText('nameless@example.test')
    ).toBeInTheDocument();

    await waitFor(() => {
      expect(localStorage.getItem(STORAGE_KEYS.USER_NAME)).toBeNull();
    });
  });

  it('clears stale admin markers when a user magic link is verified', async () => {
    localStorage.setItem(STORAGE_KEYS.ADMIN_PUBKEY, 'stale-admin-pubkey');
    localStorage.setItem(STORAGE_KEYS.ADMIN_SESSION_TOKEN, 'legacy-token');

    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/auth/verify') && init?.method === 'POST') {
        return Promise.resolve(
          Response.json({
            success: true,
            user: {
              id: 17,
              email: 'reader@example.test',
              name: 'Reader',
              user_type_id: null,
              approved: true,
              created_at: '2026-06-10 15:05:00',
              needs_onboarding: false,
              needs_user_type: false,
            },
            session_token: 'session-token',
          })
        );
      }

      return Promise.resolve(Response.json({}));
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <MemoryRouter initialEntries={['/verify?token=magic-token']}>
        <Routes>
          <Route path="/verify" element={<VerifyMagicLink />} />
          <Route path="/chat" element={<div>Chat</div>} />
        </Routes>
      </MemoryRouter>
    );

    expect(await screen.findByText('reader@example.test')).toBeInTheDocument();

    await waitFor(() => {
      expect(localStorage.getItem(STORAGE_KEYS.ADMIN_PUBKEY)).toBeNull();
      expect(localStorage.getItem(STORAGE_KEYS.ADMIN_SESSION_TOKEN)).toBeNull();
    });
  });
});
