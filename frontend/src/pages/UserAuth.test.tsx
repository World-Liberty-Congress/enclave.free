import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import i18n from '../i18n';
import {
  STORAGE_KEY_LANGUAGE,
  STORAGE_KEY_LANGUAGE_EXPLICIT,
} from '../utils/languages';
import { UserAuth } from './UserAuth';

vi.mock('../context/InstanceConfigContext', () => ({
  useInstanceConfig: () => ({
    config: {
      name: 'Enclave',
    },
  }),
}));

describe('UserAuth', () => {
  it('shows a safe cooldown for a gateway rate limit', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        new Response('<html>429 Too Many Requests</html>', {
          status: 429,
          headers: { 'Retry-After': '1' },
        })
      )
    );
    vi.stubGlobal('fetch', fetchMock);
    render(
      <MemoryRouter>
        <UserAuth />
      </MemoryRouter>
    );
    await user.type(screen.getByLabelText('Name'), 'Ada');
    await user.type(screen.getByLabelText('Email'), 'ada@example.test');
    await user.click(
      screen.getByRole('button', { name: 'Continue with Email' })
    );
    expect(
      await screen.findByRole('note', { name: 'Magic link request error' })
    ).toHaveTextContent(
      'Too many sign-in attempts. Please wait before trying again.'
    );
    expect(screen.queryByText(/<html>/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Try again in/ })).toBeDisabled();
    await waitFor(
      () =>
        expect(
          screen.getByRole('button', { name: 'Continue with Email' })
        ).toBeEnabled(),
      { timeout: 3000 }
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
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
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('lets users change language from the auth screen', async () => {
    const user = userEvent.setup();
    const previousLanguage = i18n.language;
    const changeLanguage = vi.spyOn(i18n, 'changeLanguage');

    try {
      render(
        <MemoryRouter initialEntries={['/auth']}>
          <Routes>
            <Route path="/auth" element={<UserAuth />} />
          </Routes>
        </MemoryRouter>
      );

      await user.click(screen.getByRole('button', { name: 'Change language' }));
      await user.click(screen.getByRole('menuitemradio', { name: /Español/ }));

      expect(localStorage.setItem).toHaveBeenCalledWith(
        STORAGE_KEY_LANGUAGE,
        'es'
      );
      expect(changeLanguage).toHaveBeenCalledWith('es');
    } finally {
      changeLanguage.mockRestore();
      await i18n.changeLanguage(previousLanguage);
    }
  });

  it('contains and names magic link request failures', async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          Response.json(
            { detail: 'Email service unavailable' },
            { status: 503 }
          )
        )
      )
    );

    render(
      <MemoryRouter initialEntries={['/auth']}>
        <Routes>
          <Route path="/auth" element={<UserAuth />} />
        </Routes>
      </MemoryRouter>
    );

    await user.type(screen.getByLabelText('Name'), 'Ada');
    await user.type(screen.getByLabelText('Email'), 'ada@example.com');
    await user.click(
      screen.getByRole('button', { name: 'Continue with Email' })
    );

    const failure = await screen.findByRole('note', {
      name: 'Magic link request error',
    });
    expect(failure).toHaveTextContent('Email service unavailable');
    await waitFor(() => {
      const continueButton = screen.getByRole('button', {
        name: 'Continue with Email',
      });
      expect(continueButton).toBeEnabled();
      expect(continueButton).toHaveTextContent('Continue with Email');
    });
  });

  it('sends the persisted explicit locale instead of the browser locale', async () => {
    const user = userEvent.setup();
    localStorage.setItem(STORAGE_KEY_LANGUAGE, 'es');
    localStorage.setItem(STORAGE_KEY_LANGUAGE_EXPLICIT, '1');
    vi.spyOn(window.navigator, 'language', 'get').mockReturnValue('en-US');
    const fetchMock = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) =>
      Promise.resolve(Response.json({ success: true, message: 'Sent' }))
    );
    vi.stubGlobal('fetch', fetchMock);

    render(
      <MemoryRouter initialEntries={['/auth']}>
        <Routes>
          <Route path="/auth" element={<UserAuth />} />
        </Routes>
      </MemoryRouter>
    );

    await user.type(screen.getByLabelText('Name'), 'Ada');
    await user.type(screen.getByLabelText('Email'), 'ada@example.com');
    await user.click(
      screen.getByRole('button', { name: 'Continue with Email' })
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      email: 'ada@example.com',
      name: 'Ada',
      locale: 'es',
    });
  });

  it('omits an unmarked cached locale from the magic link request', async () => {
    const user = userEvent.setup();
    localStorage.setItem(STORAGE_KEY_LANGUAGE, 'es');
    const fetchMock = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) =>
      Promise.resolve(Response.json({ success: true, message: 'Sent' }))
    );
    vi.stubGlobal('fetch', fetchMock);

    render(
      <MemoryRouter initialEntries={['/auth']}>
        <Routes>
          <Route path="/auth" element={<UserAuth />} />
        </Routes>
      </MemoryRouter>
    );

    await user.type(screen.getByLabelText('Name'), 'Ada');
    await user.type(screen.getByLabelText('Email'), 'ada@example.com');
    await user.click(
      screen.getByRole('button', { name: 'Continue with Email' })
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      email: 'ada@example.com',
      name: 'Ada',
    });
    expect(localStorage.setItem).not.toHaveBeenCalledWith(
      STORAGE_KEY_LANGUAGE_EXPLICIT,
      '1'
    );
  });
});
