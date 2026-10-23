import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { type ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ChatPage, ENCLAVE_USER_EMAIL_KEY } from './ChatPage';
import { InstanceConfigProvider } from '../context/InstanceConfigContext';
import { ThemeProvider } from '../theme';
import {
  DEFAULT_INSTANCE_CONFIG,
  INSTANCE_CONFIG_KEY,
} from '../types/instance';
import { STORAGE_KEYS } from '../types/onboarding';
import {
  adminFetch,
  isAdminAuthenticated,
  validateAdminSession,
} from '../utils/adminApi';
import { ADMIN_CONFIG_CHANGED_EVENT } from '../utils/adminConfigEvents';
import {
  sendLlmChatStreamWithUnifiedTools,
  sendLlmChatWithUnifiedTools,
} from '../utils/llmChat';
import { resetAdminResilienceInstrumentationListeners } from '../utils/adminResilienceInstrumentation';

vi.mock('../utils/adminApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../utils/adminApi')>();
  return {
    ...actual,
    adminFetch: vi.fn(),
    isAdminAuthenticated: vi.fn(() => false),
    validateAdminSession: vi.fn(() => Promise.resolve('unauthenticated')),
  };
});

vi.mock('../utils/llmChat', () => ({
  sendLlmChatStreamWithUnifiedTools: vi.fn(),
  sendLlmChatWithUnifiedTools: vi.fn(),
}));

const originalScrollIntoView = HTMLElement.prototype.scrollIntoView;

function ChatPageTestWrapper({ children }: { children: ReactNode }) {
  return (
    <MemoryRouter initialEntries={['/']}>
      <ThemeProvider>
        <InstanceConfigProvider>{children}</InstanceConfigProvider>
      </ThemeProvider>
    </MemoryRouter>
  );
}

function renderChatPageRoute() {
  return render(
    <MemoryRouter initialEntries={['/chat']}>
      <ThemeProvider>
        <InstanceConfigProvider>
          <Routes>
            <Route path="/chat" element={<ChatPage />} />
            <Route path="/pending" element={<main>Pending approval</main>} />
            <Route path="/profile" element={<main>Profile completion</main>} />
            <Route
              path="/user-type"
              element={<main>User type selection</main>}
            />
            <Route path="/login" element={<main>Login</main>} />
          </Routes>
        </InstanceConfigProvider>
      </ThemeProvider>
    </MemoryRouter>
  );
}

function setOnboardingStatusResponse(handler: () => Promise<Response>) {
  const existingFetch = vi.mocked(fetch).getMockImplementation();
  vi.mocked(fetch).mockImplementation(
    (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);

      if (url.endsWith('/users/me/onboarding-status')) {
        return handler();
      }

      return existingFetch?.(input, init) ?? Promise.resolve(Response.json({}));
    }
  );
}

function setOnboardingStatus(status: {
  needs_user_type: boolean;
  needs_onboarding: boolean;
  effective_user_type_id: number | null;
}) {
  setOnboardingStatusResponse(() => Promise.resolve(Response.json(status)));
}

describe('ChatPage', () => {
  const mockAdminFetch = vi.mocked(adminFetch);
  const mockIsAdminAuthenticated = vi.mocked(isAdminAuthenticated);
  const mockValidateAdminSession = vi.mocked(validateAdminSession);
  const rawMockAdminFetchImplementation =
    mockAdminFetch.mockImplementation.bind(mockAdminFetch);

  const defaultAdminFetch = (_endpoint: string) => null;

  async function waitForValidatedAdminConfig() {
    let configButton!: HTMLElement;
    await waitFor(() => {
      configButton = screen.getByRole('button', { name: 'Config' });
      expect(configButton).toBeInTheDocument();
    });
    if (configButton.getAttribute('aria-pressed') !== 'true') {
      fireEvent.click(configButton);
    }
    expect(configButton).toHaveAttribute('aria-pressed', 'true');
  }

  function getComposerTextbox() {
    return screen.getByRole('textbox');
  }

  beforeEach(() => {
    resetAdminResilienceInstrumentationListeners();
    mockIsAdminAuthenticated.mockReturnValue(false);
    mockValidateAdminSession.mockImplementation(() =>
      Promise.resolve(
        mockIsAdminAuthenticated() ? 'authenticated' : 'unauthenticated'
      )
    );
    mockAdminFetch.mockImplementation = ((implementation) =>
      rawMockAdminFetchImplementation((endpoint, options) => {
        const defaultResponse = defaultAdminFetch(endpoint);
        if (defaultResponse) return defaultResponse;
        return implementation(endpoint, options);
      })) as typeof mockAdminFetch.mockImplementation;
    const store = new Map<string, string>();
    vi.stubGlobal('localStorage', {
      getItem: vi.fn((key: string) => store.get(key) ?? null),
      removeItem: vi.fn((key: string) => {
        store.delete(key);
      }),
      setItem: vi.fn((key: string, value: string) => {
        store.set(key, value);
      }),
      clear: vi.fn(() => {
        store.clear();
      }),
    });
    localStorage.setItem('enclave-theme', 'light');
    localStorage.setItem(
      INSTANCE_CONFIG_KEY,
      JSON.stringify(DEFAULT_INSTANCE_CONFIG)
    );
    localStorage.setItem(ENCLAVE_USER_EMAIL_KEY, 'reader@example.com');
    vi.mocked(isAdminAuthenticated).mockReturnValue(false);
    vi.mocked(adminFetch).mockImplementation((endpoint: string) => {
      return defaultAdminFetch(endpoint) ?? Promise.resolve(Response.json({}));
    });

    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);

        if (url.endsWith('/settings/public')) {
          return Promise.resolve(Response.json({ settings: {} }));
        }

        if (url.endsWith('/session-defaults')) {
          return Promise.resolve(
            Response.json({
              web_search_enabled: true,
              default_document_ids: ['doc-1'],
              default_tool_ids: ['curated-resources', 'web-search'],
              knowledge_source_scope: 'selected',
            })
          );
        }

        if (url.endsWith('/query/sessions')) {
          return Promise.resolve(Response.json({ conversations: [] }));
        }

        if (url.endsWith('/ingest/jobs')) {
          return Promise.resolve(
            Response.json({
              jobs: [
                {
                  job_id: 'doc-1',
                  filename: 'operator-handbook.pdf',
                  status: 'completed',
                  total_chunks: 12,
                },
                {
                  job_id: 'doc-2',
                  filename: 'user-faq.md',
                  status: 'completed',
                  total_chunks: 4,
                },
              ],
            })
          );
        }

        if (url.endsWith('/users/me/onboarding-status')) {
          return Promise.resolve(
            Response.json({
              needs_user_type: false,
              needs_onboarding: false,
              effective_user_type_id: null,
            })
          );
        }

        return Promise.resolve(Response.json({}));
      })
    );
    vi.stubGlobal(
      'matchMedia',
      vi.fn().mockReturnValue({
        matches: false,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      })
    );
    HTMLElement.prototype.scrollIntoView = vi.fn();
  });

  afterEach(() => {
    resetAdminResilienceInstrumentationListeners();
    cleanup();
    vi.unstubAllGlobals();
    HTMLElement.prototype.scrollIntoView = originalScrollIntoView;
    vi.clearAllMocks();
    vi.useRealTimers();
    document.documentElement.classList.remove('dark');
  });

  it('loads configured user defaults without showing Tool controls', async () => {
    render(<ChatPage />, { wrapper: ChatPageTestWrapper });

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        expect.stringMatching(/^\/api\/session-defaults(?:\?|$)/)
      );
    });

    expect(
      screen.queryByRole('button', { name: 'Web' })
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Resources' })
    ).not.toBeInTheDocument();
  });

  it('routes pending-approval users to User Type selection before pending approval on direct chat entry', async () => {
    localStorage.setItem(ENCLAVE_USER_EMAIL_KEY, 'pending-type@example.com');
    localStorage.setItem(STORAGE_KEYS.USER_APPROVED, 'false');
    setOnboardingStatus({
      needs_user_type: true,
      needs_onboarding: true,
      effective_user_type_id: null,
    });

    renderChatPageRoute();

    expect(await screen.findByText('User type selection')).toBeInTheDocument();
    expect(screen.queryByText('Pending approval')).not.toBeInTheDocument();
  });

  it('routes pending-approval users to profile completion before pending approval on direct chat entry', async () => {
    localStorage.setItem(ENCLAVE_USER_EMAIL_KEY, 'pending-profile@example.com');
    localStorage.setItem(STORAGE_KEYS.USER_APPROVED, 'false');
    setOnboardingStatus({
      needs_user_type: false,
      needs_onboarding: true,
      effective_user_type_id: 7,
    });

    renderChatPageRoute();

    expect(await screen.findByText('Profile completion')).toBeInTheDocument();
    expect(screen.queryByText('Pending approval')).not.toBeInTheDocument();
    expect(localStorage.getItem(STORAGE_KEYS.USER_TYPE_ID)).toBe('7');
  });

  it('routes pending-approval users to pending approval once onboarding is complete on direct chat entry', async () => {
    localStorage.setItem(ENCLAVE_USER_EMAIL_KEY, 'pending-ready@example.com');
    localStorage.setItem(STORAGE_KEYS.USER_APPROVED, 'false');
    setOnboardingStatus({
      needs_user_type: false,
      needs_onboarding: false,
      effective_user_type_id: null,
    });

    renderChatPageRoute();

    expect(await screen.findByText('Pending approval')).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledWith(
      expect.stringMatching(/\/users\/me\/onboarding-status$/),
      expect.objectContaining({ credentials: 'include' })
    );
  });

  it('routes pending-approval users to pending approval when onboarding status returns an error', async () => {
    localStorage.setItem(ENCLAVE_USER_EMAIL_KEY, 'pending-error@example.com');
    localStorage.setItem(STORAGE_KEYS.USER_APPROVED, 'false');
    setOnboardingStatusResponse(() =>
      Promise.resolve(Response.json({}, { status: 500 }))
    );

    renderChatPageRoute();

    expect(await screen.findByText('Pending approval')).toBeInTheDocument();
  });

  it('routes pending-approval users to pending approval when onboarding status cannot be fetched', async () => {
    localStorage.setItem(ENCLAVE_USER_EMAIL_KEY, 'pending-network@example.com');
    localStorage.setItem(STORAGE_KEYS.USER_APPROVED, 'false');
    setOnboardingStatusResponse(() =>
      Promise.reject(new Error('network down'))
    );

    renderChatPageRoute();

    expect(await screen.findByText('Pending approval')).toBeInTheDocument();
  });

  it('sends configured default user Tool Sets without exposing controls', async () => {
    const user = userEvent.setup();
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementationOnce(
      async ({ onEvent }) => {
        onEvent('assistant_message_started', {
          message_id: 'msg-resources',
          session_id: 'session-1',
        });
        onEvent('answer_delta', {
          message_id: 'msg-resources',
          delta: 'Try these vetted resources.',
        });
        onEvent('done', {
          message_id: 'msg-resources',
          session_id: 'session-1',
        });
      }
    );

    render(<ChatPage />, { wrapper: ChatPageTestWrapper });

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        expect.stringMatching(/^\/api\/session-defaults(?:\?|$)/)
      );
    });
    expect(
      screen.queryByRole('button', { name: 'Resources' })
    ).not.toBeInTheDocument();

    await user.type(getComposerTextbox(), 'Who can help in Nicaragua?');
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    await waitFor(() => {
      expect(sendLlmChatStreamWithUnifiedTools).toHaveBeenCalled();
    });

    const callArgs = vi.mocked(sendLlmChatStreamWithUnifiedTools).mock
      .calls[0][0];
    expect(callArgs.tools).toContain('curated-resources');
    expect(callArgs.tools).toContain('web-search');
    expect(callArgs.tools).not.toContain('admin-config');
    expect(callArgs.tools).not.toContain('db-query');
  });

  it('sends configured selected knowledge sources without exposing document controls', async () => {
    const user = userEvent.setup();
    vi.mocked(fetch).mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);

      if (url.endsWith('/settings/public')) {
        return Promise.resolve(Response.json({ settings: {} }));
      }

      if (url.endsWith('/session-defaults')) {
        return Promise.resolve(
          Response.json({
            web_search_enabled: true,
            default_document_ids: ['doc-1'],
            default_tool_ids: ['curated-resources', 'web-search'],
            knowledge_source_scope: 'selected',
          })
        );
      }

      if (url.endsWith('/query/sessions')) {
        return Promise.resolve(Response.json({ conversations: [] }));
      }

      if (url.endsWith('/ingest/jobs')) {
        return Promise.resolve(
          Response.json({
            jobs: [
              {
                job_id: 'doc-1',
                filename: 'operator-handbook.pdf',
                status: 'completed',
                total_chunks: 12,
              },
            ],
          })
        );
      }

      if (url.endsWith('/users/me/onboarding-status')) {
        return Promise.resolve(
          Response.json({
            needs_user_type: false,
            needs_onboarding: false,
            effective_user_type_id: null,
          })
        );
      }

      return Promise.resolve(Response.json({}));
    });
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementationOnce(
      async ({ onEvent }) => {
        onEvent('assistant_message_started', {
          message_id: 'msg-docs',
          session_id: 'session-1',
        });
        onEvent('answer_delta', {
          message_id: 'msg-docs',
          delta: 'The handbook answer.',
        });
        onEvent('done', { message_id: 'msg-docs', session_id: 'session-1' });
      }
    );

    render(<ChatPage />, { wrapper: ChatPageTestWrapper });

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        expect.stringMatching(/^\/api\/session-defaults(?:\?|$)/)
      );
    });
    expect(
      screen.queryByRole('button', { name: 'Docs 1' })
    ).not.toBeInTheDocument();
    await user.type(getComposerTextbox(), 'What does the handbook say?');
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    expect(await screen.findByText('The handbook answer.')).toBeInTheDocument();
    expect(sendLlmChatStreamWithUnifiedTools).toHaveBeenCalledWith(
      expect.objectContaining({
        content: 'What does the handbook say?',
        tools: expect.arrayContaining([
          'web-search',
          'knowledge-search',
          'curated-resources',
        ]),
        jobIds: ['doc-1'],
      })
    );
    expect(fetch).not.toHaveBeenCalledWith(
      expect.stringMatching(/\/query$/),
      expect.anything()
    );
    expect(fetch).not.toHaveBeenCalledWith(
      expect.stringMatching(/\/query\/stream$/),
      expect.anything()
    );
  });

  it('hides the user composer Tool and Document controls', async () => {
    render(<ChatPage />, { wrapper: ChatPageTestWrapper });

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        expect.stringMatching(/^\/api\/session-defaults(?:\?|$)/)
      );
    });
    expect(
      screen.queryByRole('region', { name: 'Composer context' })
    ).not.toBeInTheDocument();
    expect(screen.queryByText('Tools')).not.toBeInTheDocument();
    expect(screen.queryByText('Documents')).not.toBeInTheDocument();
    expect(
      document.querySelector('input[type="file"]')
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /attach|upload/i })
    ).not.toBeInTheDocument();
  });

  it('uses the shared Conversation Surface for User Conversations while preserving surrounding controls', async () => {
    vi.mocked(fetch).mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);

      if (url.endsWith('/settings/public')) {
        return Promise.resolve(
          Response.json({
            settings: {
              reachout_enabled: 'true',
              reachout_mode: 'help',
            },
          })
        );
      }

      if (url.endsWith('/session-defaults')) {
        return Promise.resolve(
          Response.json({
            web_search_enabled: true,
            default_document_ids: ['doc-1'],
            default_tool_ids: ['curated-resources', 'web-search'],
            knowledge_source_scope: 'selected',
          })
        );
      }

      if (url.endsWith('/ingest/jobs')) {
        return Promise.resolve(
          Response.json({
            jobs: [
              {
                job_id: 'doc-1',
                filename: 'operator-handbook.pdf',
                status: 'completed',
                total_chunks: 12,
              },
            ],
          })
        );
      }

      if (url.endsWith('/users/me/onboarding-status')) {
        return Promise.resolve(
          Response.json({
            needs_user_type: false,
            needs_onboarding: false,
            effective_user_type_id: null,
          })
        );
      }

      return Promise.resolve(Response.json({}));
    });

    render(<ChatPage />, { wrapper: ChatPageTestWrapper });

    expect(
      await screen.findByRole('region', { name: 'Conversation surface' })
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Web' })
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Docs 1' })
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Get help' })
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Export' })).toHaveAttribute(
      'title',
      'No messages to export'
    );
  });

  it('shows a session sidebar shell and starts a fresh chat from it', async () => {
    const user = userEvent.setup();
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementationOnce(
      async ({ onEvent }) => {
        onEvent('assistant_message_started', {
          message_id: 'msg-stream',
          session_id: 'session-1',
        });
        onEvent('answer_delta', {
          message_id: 'msg-stream',
          delta: 'First answer.',
        });
        onEvent('done', { message_id: 'msg-stream', session_id: 'session-1' });
      }
    );

    render(<ChatPage />, { wrapper: ChatPageTestWrapper });

    expect(
      await screen.findByRole('navigation', { name: 'Chat sessions' })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Open chat sessions' })
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole('button', { name: 'Open chat sessions' })
    );
    expect(
      screen.getAllByRole('navigation', { name: 'Chat sessions' })
    ).toHaveLength(2);
    await user.click(
      screen.getByRole('button', { name: 'Close chat sessions' })
    );
    expect(
      screen.getAllByRole('navigation', { name: 'Chat sessions' })
    ).toHaveLength(1);
    expect(
      screen.getByRole('button', { name: 'New chat' })
    ).toBeInTheDocument();
    const currentChatBar = screen.getByRole('region', {
      name: 'Current chat',
    });
    expect(
      within(currentChatBar).getByRole('button', { name: 'Start a new chat' })
    ).toBeInTheDocument();
    expect(screen.getAllByText('Current chat').length).toBeGreaterThan(0);

    await user.type(getComposerTextbox(), 'Hello');
    await user.click(screen.getByRole('button', { name: 'Send message' }));
    expect(await screen.findByText('First answer.')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Hello 2 messages' })
    ).toHaveAttribute('aria-current', 'page');

    await user.click(
      screen.getByRole('button', { name: 'Open chat sessions' })
    );
    const mobileSessions = screen.getAllByRole('navigation', {
      name: 'Chat sessions',
    })[1];
    await user.click(
      within(mobileSessions).getByRole('button', { name: 'New chat' })
    );

    expect(screen.queryByText('First answer.')).not.toBeInTheDocument();
    expect(
      screen.getAllByRole('navigation', { name: 'Chat sessions' })
    ).toHaveLength(1);
    expect(
      screen.getByRole('button', { name: 'Current chat Empty' })
    ).toHaveAttribute('aria-current', 'page');
    expect(
      screen.getByRole('heading', { name: 'What would you like to know?' })
    ).toBeInTheDocument();
  });

  it('lists durable Conversations in the session sidebar', async () => {
    vi.mocked(fetch).mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);

      if (url.endsWith('/settings/public')) {
        return Promise.resolve(Response.json({ settings: {} }));
      }

      if (url.endsWith('/session-defaults')) {
        return Promise.resolve(
          Response.json({
            web_search_enabled: true,
            default_document_ids: [],
          })
        );
      }

      if (url.endsWith('/query/sessions')) {
        return Promise.resolve(
          Response.json({
            conversations: [
              {
                id: 'session-1',
                title: 'Draft membership policy',
                updated_at: '2026-05-24T20:00:00Z',
                message_count: 4,
              },
            ],
          })
        );
      }

      if (url.endsWith('/ingest/jobs')) {
        return Promise.resolve(Response.json({ jobs: [] }));
      }

      if (url.endsWith('/users/me/onboarding-status')) {
        return Promise.resolve(
          Response.json({
            needs_user_type: false,
            needs_onboarding: false,
            effective_user_type_id: null,
          })
        );
      }

      return Promise.resolve(Response.json({}));
    });

    render(<ChatPage />, { wrapper: ChatPageTestWrapper });

    const sidebar = await screen.findByRole('navigation', {
      name: 'Chat sessions',
    });

    expect(
      within(sidebar).getByRole('button', {
        name: 'Draft membership policy 4 messages',
      })
    ).toBeInTheDocument();
  });

  it('hydrates persisted Conversation Trace Deltas when resuming a saved Conversation', async () => {
    const user = userEvent.setup();
    vi.mocked(fetch).mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);

      if (url.endsWith('/settings/public')) {
        return Promise.resolve(Response.json({ settings: {} }));
      }

      if (url.endsWith('/session-defaults')) {
        return Promise.resolve(
          Response.json({
            web_search_enabled: false,
            default_document_ids: [],
          })
        );
      }

      if (url.endsWith('/query/sessions')) {
        return Promise.resolve(
          Response.json({
            conversations: [
              {
                id: 'session-1',
                title: 'Check deployment settings',
                updated_at: '2026-06-18T12:00:00Z',
                message_count: 2,
              },
            ],
          })
        );
      }

      if (url.endsWith('/query/session/session-1')) {
        return Promise.resolve(
          Response.json({
            id: 'session-1',
            title: 'Check deployment settings',
            messages: [
              {
                id: 'msg-user',
                role: 'user',
                content: 'Check the settings.',
              },
              {
                id: 'msg-assistant',
                role: 'assistant',
                content: 'Settings are ready.',
                trace_deltas: [
                  {
                    id: 'trace-toplevel-config',
                    kind: 'tool_result',
                    title: 'Top Level Admin Config',
                    tool_name: 'read_instance_settings',
                    status: 'succeeded',
                    content: 'Top-level tool completed.',
                    metadata: { duration_ms: 42 },
                  },
                ],
                trace: {
                  visibility: 'detailed',
                  reasoning: {
                    summary: 'Sage checked configuration before answering.',
                  },
                  trace_deltas: [
                    {
                      id: 'trace-nested-config',
                      kind: 'tool_result',
                      title: 'Nested Admin Config',
                      tool_name: 'read_nested_settings',
                      status: 'succeeded',
                      content: 'Nested tool completed.',
                      metadata: { duration_ms: 42 },
                    },
                  ],
                  tools: [],
                  retrieval: [],
                  activity_steps: [],
                  suppressed: false,
                },
              },
            ],
          })
        );
      }

      if (url.endsWith('/ingest/jobs')) {
        return Promise.resolve(Response.json({ jobs: [] }));
      }

      if (url.endsWith('/users/me/onboarding-status')) {
        return Promise.resolve(
          Response.json({
            needs_user_type: false,
            needs_onboarding: false,
            effective_user_type_id: null,
          })
        );
      }

      return Promise.resolve(Response.json({}));
    });

    render(<ChatPage />, { wrapper: ChatPageTestWrapper });

    await user.click(
      await screen.findByRole('button', {
        name: 'Check deployment settings 2 messages',
      })
    );

    expect(await screen.findByText('Settings are ready.')).toBeInTheDocument();
    expect(screen.getByText('Top Level Admin Config')).toBeInTheDocument();
    expect(screen.getByText('read_instance_settings')).toBeInTheDocument();
    expect(screen.getByText('Top-level tool completed.')).toBeInTheDocument();
    expect(screen.queryByText('Nested Admin Config')).not.toBeInTheDocument();
    expect(
      screen.queryByText('Nested tool completed.')
    ).not.toBeInTheDocument();
  });

  it('rejects malformed resumed Conversations instead of clearing the thread', async () => {
    const user = userEvent.setup();
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementationOnce(
      async ({ onEvent }) => {
        onEvent('assistant_message_started', {
          message_id: 'msg-local',
          session_id: 'local-session',
        });
        onEvent('answer_delta', {
          message_id: 'msg-local',
          delta: 'Local answer remains.',
        });
        onEvent('done', {
          message_id: 'msg-local',
          session_id: 'local-session',
        });
      }
    );

    vi.mocked(fetch).mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);

      if (url.endsWith('/settings/public')) {
        return Promise.resolve(Response.json({ settings: {} }));
      }

      if (url.endsWith('/session-defaults')) {
        return Promise.resolve(
          Response.json({
            web_search_enabled: true,
            default_document_ids: [],
          })
        );
      }

      if (url.endsWith('/query/sessions')) {
        return Promise.resolve(
          Response.json({
            conversations: [
              {
                id: 'session-1',
                title: 'Malformed saved chat',
                updated_at: '2026-05-24T20:00:00Z',
                message_count: 2,
              },
            ],
          })
        );
      }

      if (url.endsWith('/query/session/session-1')) {
        return Promise.resolve(
          Response.json({
            id: 'session-1',
            title: 'Malformed saved chat',
            messages: null,
          })
        );
      }

      if (url.endsWith('/ingest/jobs')) {
        return Promise.resolve(Response.json({ jobs: [] }));
      }

      if (url.endsWith('/users/me/onboarding-status')) {
        return Promise.resolve(
          Response.json({
            needs_user_type: false,
            needs_onboarding: false,
            effective_user_type_id: null,
          })
        );
      }

      return Promise.resolve(Response.json({}));
    });

    render(<ChatPage />, { wrapper: ChatPageTestWrapper });

    await user.type(getComposerTextbox(), 'Keep this local thread');
    await user.click(screen.getByRole('button', { name: 'Send message' }));
    expect(
      await screen.findByText('Local answer remains.')
    ).toBeInTheDocument();

    const sidebar = await screen.findByRole('navigation', {
      name: 'Chat sessions',
    });
    await user.click(
      within(sidebar).getByRole('button', {
        name: 'Malformed saved chat 2 messages',
      })
    );

    expect(
      await screen.findByRole('note', { name: 'Chat request error' })
    ).toHaveTextContent('Unable to load that Conversation.');
    expect(screen.getByText('Local answer remains.')).toBeInTheDocument();
  });

  it('resumes a selected durable Conversation and continues the same session on the next turn', async () => {
    const user = userEvent.setup();
    vi.mocked(fetch).mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);

      if (url.endsWith('/settings/public')) {
        return Promise.resolve(Response.json({ settings: {} }));
      }

      if (url.endsWith('/session-defaults')) {
        return Promise.resolve(
          Response.json({
            web_search_enabled: true,
            default_document_ids: [],
          })
        );
      }

      if (url.endsWith('/query/sessions')) {
        return Promise.resolve(
          Response.json({
            conversations: [
              {
                id: 'session-1',
                title: 'Draft membership policy',
                updated_at: '2026-05-24T20:00:00Z',
                message_count: 2,
              },
            ],
          })
        );
      }

      if (url.endsWith('/query/session/session-1')) {
        return Promise.resolve(
          Response.json({
            id: 'session-1',
            title: 'Draft membership policy',
            messages: [
              {
                id: 'msg-user-1',
                role: 'user',
                content: 'What should membership include?',
              },
              {
                id: 'msg-assistant-1',
                role: 'assistant',
                content: 'Membership should include clear renewal terms.',
                activity_steps: [
                  {
                    id: 'knowledge-search',
                    kind: 'tool',
                    title: 'Knowledge Search',
                    status: 'completed',
                    summary: 'Checked uploaded policy material.',
                  },
                ],
                trace: {
                  visibility: 'summary',
                  tools: [
                    {
                      id: 'knowledge-search',
                      name: 'Knowledge Search',
                      status: 'completed',
                    },
                  ],
                  activity_steps: [
                    {
                      id: 'knowledge-search',
                      kind: 'tool',
                      title: 'Knowledge Search',
                      status: 'completed',
                      summary: 'Checked uploaded policy material.',
                    },
                  ],
                },
              },
            ],
          })
        );
      }

      if (url.endsWith('/ingest/jobs')) {
        return Promise.resolve(Response.json({ jobs: [] }));
      }

      if (url.endsWith('/users/me/onboarding-status')) {
        return Promise.resolve(
          Response.json({
            needs_user_type: false,
            needs_onboarding: false,
            effective_user_type_id: null,
          })
        );
      }

      return Promise.resolve(Response.json({}));
    });
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementationOnce(
      async ({ sessionId, onEvent }) => {
        expect(sessionId).toBe('session-1');
        onEvent('assistant_message_started', {
          message_id: 'msg-assistant-2',
          session_id: 'session-1',
        });
        onEvent('answer_delta', {
          message_id: 'msg-assistant-2',
          delta: 'Continued in the saved Conversation.',
        });
        onEvent('done', {
          message_id: 'msg-assistant-2',
          session_id: 'session-1',
        });
      }
    );

    render(<ChatPage />, { wrapper: ChatPageTestWrapper });

    const sidebar = await screen.findByRole('navigation', {
      name: 'Chat sessions',
    });
    await user.click(
      within(sidebar).getByRole('button', {
        name: 'Draft membership policy 2 messages',
      })
    );

    expect(
      await screen.findByText('What should membership include?')
    ).toBeInTheDocument();
    expect(
      screen.getByText('Membership should include clear renewal terms.')
    ).toBeInTheDocument();
    expect(screen.getAllByText('Knowledge Search').length).toBeGreaterThan(0);
    expect(
      within(sidebar).getByRole('button', {
        name: 'Draft membership policy 2 messages',
      })
    ).toHaveAttribute('aria-current', 'page');
    expect(
      screen.getByRole('region', { name: 'Current chat' })
    ).toHaveTextContent('Draft membership policy');

    await user.type(getComposerTextbox(), 'What comes next?');
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    expect(
      await screen.findByText('Continued in the saved Conversation.')
    ).toBeInTheDocument();
    expect(sendLlmChatStreamWithUnifiedTools).toHaveBeenCalledTimes(1);
  });

  it('keeps server history titles and preserves the list when refresh fails', async () => {
    const user = userEvent.setup();
    let historyCalls = 0;
    vi.mocked(fetch).mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);

      if (url.endsWith('/settings/public')) {
        return Promise.resolve(Response.json({ settings: {} }));
      }

      if (url.endsWith('/session-defaults')) {
        return Promise.resolve(
          Response.json({
            web_search_enabled: true,
            default_document_ids: [],
          })
        );
      }

      if (url.endsWith('/query/sessions')) {
        historyCalls += 1;
        if (historyCalls > 1) {
          return Promise.resolve(Response.json({}, { status: 503 }));
        }
        return Promise.resolve(
          Response.json({
            conversations: [
              {
                id: 'session-1',
                title: 'Server saved title',
                updated_at: '2026-05-24T20:00:00Z',
                message_count: 2,
              },
            ],
          })
        );
      }

      if (url.endsWith('/query/session/session-1')) {
        return Promise.resolve(
          Response.json({
            id: 'session-1',
            title: 'Stale hydrated title',
            messages: [
              {
                id: 'msg-user-1',
                role: 'user',
                content: 'Existing question',
              },
              {
                id: 'msg-assistant-1',
                role: 'assistant',
                content: 'Existing answer with malformed trace.',
                trace: {
                  visibility: 'summary',
                  tools: 'not-an-array',
                },
              },
            ],
          })
        );
      }

      if (url.endsWith('/ingest/jobs')) {
        return Promise.resolve(Response.json({ jobs: [] }));
      }

      if (url.endsWith('/users/me/onboarding-status')) {
        return Promise.resolve(
          Response.json({
            needs_user_type: false,
            needs_onboarding: false,
            effective_user_type_id: null,
          })
        );
      }

      return Promise.resolve(Response.json({}));
    });
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementationOnce(
      async ({ sessionId, onEvent }) => {
        expect(sessionId).toBe('session-1');
        onEvent('assistant_message_started', {
          message_id: 'msg-assistant-2',
          session_id: 'session-1',
        });
        onEvent('answer_delta', {
          message_id: 'msg-assistant-2',
          delta: 'Refresh failed but list stayed.',
        });
        onEvent('done', {
          message_id: 'msg-assistant-2',
          session_id: 'session-1',
        });
      }
    );

    render(<ChatPage />, { wrapper: ChatPageTestWrapper });

    const sidebar = await screen.findByRole('navigation', {
      name: 'Chat sessions',
    });
    await user.click(
      within(sidebar).getByRole('button', {
        name: 'Server saved title 2 messages',
      })
    );

    expect(
      screen.getByRole('region', { name: 'Current chat' })
    ).toHaveTextContent('Server saved title');
    expect(
      await screen.findByText('Existing answer with malformed trace.')
    ).toBeInTheDocument();

    await user.type(getComposerTextbox(), 'Continue');
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    expect(
      await screen.findByText('Refresh failed but list stayed.')
    ).toBeInTheDocument();
    expect(
      within(sidebar).getByRole('button', {
        name: 'Server saved title 2 messages',
      })
    ).toHaveAttribute('aria-current', 'page');
  });

  it('starts a durable-history-aware New Chat without deleting saved Conversations', async () => {
    const user = userEvent.setup();
    let historyCalls = 0;
    vi.mocked(fetch).mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);

      if (url.endsWith('/settings/public')) {
        return Promise.resolve(Response.json({ settings: {} }));
      }

      if (url.endsWith('/session-defaults')) {
        return Promise.resolve(
          Response.json({
            web_search_enabled: true,
            default_document_ids: [],
          })
        );
      }

      if (url.endsWith('/query/sessions')) {
        historyCalls += 1;
        return Promise.resolve(
          Response.json({
            conversations:
              historyCalls === 1
                ? [
                    {
                      id: 'session-1',
                      title: 'Existing policy chat',
                      updated_at: '2026-05-24T20:00:00Z',
                      message_count: 2,
                    },
                  ]
                : [
                    {
                      id: 'session-2',
                      title: 'Start separate thread',
                      updated_at: '2026-05-24T20:05:00Z',
                      message_count: 2,
                    },
                    {
                      id: 'session-1',
                      title: 'Existing policy chat',
                      updated_at: '2026-05-24T20:00:00Z',
                      message_count: 2,
                    },
                  ],
          })
        );
      }

      if (url.endsWith('/query/session/session-1')) {
        return Promise.resolve(
          Response.json({
            id: 'session-1',
            title: 'Existing policy chat',
            messages: [
              {
                id: 'msg-user-1',
                role: 'user',
                content: 'Existing question',
              },
              {
                id: 'msg-assistant-1',
                role: 'assistant',
                content: 'Existing answer',
              },
            ],
          })
        );
      }

      if (url.endsWith('/ingest/jobs')) {
        return Promise.resolve(Response.json({ jobs: [] }));
      }

      if (url.endsWith('/users/me/onboarding-status')) {
        return Promise.resolve(
          Response.json({
            needs_user_type: false,
            needs_onboarding: false,
            effective_user_type_id: null,
          })
        );
      }

      return Promise.resolve(Response.json({}));
    });
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementationOnce(
      async ({ sessionId, onEvent }) => {
        expect(sessionId).toBeNull();
        onEvent('assistant_message_started', {
          message_id: 'msg-assistant-2',
          session_id: 'session-2',
        });
        onEvent('answer_delta', {
          message_id: 'msg-assistant-2',
          delta: 'Fresh answer',
        });
        onEvent('done', {
          message_id: 'msg-assistant-2',
          session_id: 'session-2',
        });
      }
    );

    render(<ChatPage />, { wrapper: ChatPageTestWrapper });

    const sidebar = await screen.findByRole('navigation', {
      name: 'Chat sessions',
    });
    await user.click(
      within(sidebar).getByRole('button', {
        name: 'Existing policy chat 2 messages',
      })
    );
    expect(await screen.findByText('Existing answer')).toBeInTheDocument();

    await user.click(within(sidebar).getByRole('button', { name: 'New chat' }));
    expect(screen.queryByText('Existing answer')).not.toBeInTheDocument();
    expect(
      within(sidebar).getByRole('button', {
        name: 'Existing policy chat 2 messages',
      })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Current chat Empty' })
    ).toHaveAttribute('aria-current', 'page');

    await user.type(getComposerTextbox(), 'Start separate thread');
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    expect(await screen.findByText('Fresh answer')).toBeInTheDocument();
    expect(
      await within(sidebar).findByRole('button', {
        name: 'Start separate thread 2 messages',
      })
    ).toHaveAttribute('aria-current', 'page');
    expect(
      within(sidebar).getByRole('button', {
        name: 'Existing policy chat 2 messages',
      })
    ).toBeInTheDocument();
  });

  it('renames the active durable Conversation and keeps the hydrated turns intact', async () => {
    const user = userEvent.setup();
    vi.mocked(fetch).mockImplementation((input: RequestInfo | URL, init) => {
      const url = String(input);

      if (url.endsWith('/settings/public')) {
        return Promise.resolve(Response.json({ settings: {} }));
      }

      if (url.endsWith('/session-defaults')) {
        return Promise.resolve(
          Response.json({
            web_search_enabled: true,
            default_document_ids: [],
          })
        );
      }

      if (url.endsWith('/query/sessions')) {
        return Promise.resolve(
          Response.json({
            conversations: [
              {
                id: 'session-1',
                title: 'Draft membership policy',
                updated_at: '2026-05-24T20:00:00Z',
                message_count: 2,
              },
            ],
          })
        );
      }

      if (
        url.endsWith('/query/session/session-1') &&
        init?.method === 'PATCH'
      ) {
        expect(JSON.parse(String(init.body))).toEqual({
          title: 'Board policy',
        });
        return Promise.resolve(
          Response.json({
            id: 'session-1',
            title: 'Board policy',
            message_count: 2,
          })
        );
      }

      if (url.endsWith('/query/session/session-1')) {
        return Promise.resolve(
          Response.json({
            id: 'session-1',
            title: 'Draft membership policy',
            messages: [
              {
                id: 'msg-user-1',
                role: 'user',
                content: 'Existing question',
              },
              {
                id: 'msg-assistant-1',
                role: 'assistant',
                content: 'Existing answer',
              },
            ],
          })
        );
      }

      if (url.endsWith('/ingest/jobs')) {
        return Promise.resolve(Response.json({ jobs: [] }));
      }

      if (url.endsWith('/users/me/onboarding-status')) {
        return Promise.resolve(
          Response.json({
            needs_user_type: false,
            needs_onboarding: false,
            effective_user_type_id: null,
          })
        );
      }

      return Promise.resolve(Response.json({}));
    });

    render(<ChatPage />, { wrapper: ChatPageTestWrapper });

    const sidebar = await screen.findByRole('navigation', {
      name: 'Chat sessions',
    });
    await user.click(
      within(sidebar).getByRole('button', {
        name: 'Draft membership policy 2 messages',
      })
    );
    expect(await screen.findByText('Existing answer')).toBeInTheDocument();

    await user.click(
      within(sidebar).getByRole('button', {
        name: 'Rename Draft membership policy',
      })
    );
    const titleInput = within(sidebar).getByRole('textbox', {
      name: 'Conversation title',
    });
    await user.clear(titleInput);
    await user.type(titleInput, 'Board policy');
    await user.click(
      within(sidebar).getByRole('button', {
        name: 'Save conversation title',
      })
    );

    expect(
      await within(sidebar).findByRole('button', {
        name: 'Board policy 2 messages',
      })
    ).toHaveAttribute('aria-current', 'page');
    expect(
      screen.getByRole('region', { name: 'Current chat' })
    ).toHaveTextContent('Board policy');
    expect(screen.getByText('Existing answer')).toBeInTheDocument();
  });

  it('deletes the active durable Conversation from the sidebar without exposing turn contents in the confirmation', async () => {
    const user = userEvent.setup();
    const conversations = [
      {
        id: 'session-1',
        title: 'Safe board policy title',
        updated_at: '2026-05-24T20:00:00Z',
        message_count: 2,
      },
    ];
    vi.mocked(fetch).mockImplementation((input: RequestInfo | URL, init) => {
      const url = String(input);

      if (url.endsWith('/settings/public')) {
        return Promise.resolve(Response.json({ settings: {} }));
      }

      if (url.endsWith('/session-defaults')) {
        return Promise.resolve(
          Response.json({
            web_search_enabled: true,
            default_document_ids: [],
          })
        );
      }

      if (url.endsWith('/query/sessions')) {
        return Promise.resolve(Response.json({ conversations }));
      }

      if (
        url.endsWith('/query/session/session-1') &&
        init?.method === 'DELETE'
      ) {
        conversations.length = 0;
        return Promise.resolve(
          Response.json({
            status: 'deleted',
            deletion: {
              session_id: 'session-1',
              message_count: 2,
              deleted_at: '2026-05-24T20:05:00Z',
            },
          })
        );
      }

      if (url.endsWith('/query/session/session-1')) {
        return Promise.resolve(
          Response.json({
            id: 'session-1',
            title: 'Safe board policy title',
            messages: [
              {
                id: 'msg-user-1',
                role: 'user',
                content: 'Raw question with secret@example.com inside',
              },
              {
                id: 'msg-assistant-1',
                role: 'assistant',
                content: 'Private answer that should never enter confirmation',
              },
            ],
          })
        );
      }

      if (url.endsWith('/ingest/jobs')) {
        return Promise.resolve(Response.json({ jobs: [] }));
      }

      if (url.endsWith('/users/me/onboarding-status')) {
        return Promise.resolve(
          Response.json({
            needs_user_type: false,
            needs_onboarding: false,
            effective_user_type_id: null,
          })
        );
      }

      return Promise.resolve(Response.json({}));
    });

    render(<ChatPage />, { wrapper: ChatPageTestWrapper });

    const sidebar = await screen.findByRole('navigation', {
      name: 'Chat sessions',
    });
    await user.click(
      within(sidebar).getByRole('button', {
        name: 'Safe board policy title 2 messages',
      })
    );
    expect(
      await screen.findByText(
        'Private answer that should never enter confirmation'
      )
    ).toBeInTheDocument();

    await user.click(
      within(sidebar).getByRole('button', {
        name: 'Delete Safe board policy title',
      })
    );
    expect(
      within(sidebar).getByText('Delete "Safe board policy title"?')
    ).toBeInTheDocument();
    expect(
      within(sidebar).queryByText(/secret@example.com/)
    ).not.toBeInTheDocument();
    expect(
      within(sidebar).queryByText(
        /Private answer that should never enter confirmation/
      )
    ).not.toBeInTheDocument();

    await user.click(
      within(sidebar).getByRole('button', {
        name: 'Cancel conversation delete',
      })
    );
    expect(
      within(sidebar).getByRole('button', {
        name: 'Safe board policy title 2 messages',
      })
    ).toHaveAttribute('aria-current', 'page');
    expect(
      screen.getByText('Private answer that should never enter confirmation')
    ).toBeInTheDocument();

    await user.click(
      within(sidebar).getByRole('button', {
        name: 'Delete Safe board policy title',
      })
    );
    await user.click(
      within(sidebar).getByRole('button', {
        name: 'Delete conversation',
      })
    );

    await waitFor(() => {
      expect(
        within(sidebar).queryByRole('button', {
          name: 'Safe board policy title 2 messages',
        })
      ).not.toBeInTheDocument();
    });
    expect(
      screen.queryByText('Private answer that should never enter confirmation')
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Current chat Empty' })
    ).toHaveAttribute('aria-current', 'page');
  });

  it('contains chat request failures in a named error note', async () => {
    const user = (await import('@testing-library/user-event')).default.setup();
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockRejectedValueOnce(
      new Error('Stream unavailable')
    );
    vi.mocked(sendLlmChatWithUnifiedTools).mockRejectedValueOnce(
      new Error('Model gateway unavailable')
    );

    render(<ChatPage />, { wrapper: ChatPageTestWrapper });

    await user.type(getComposerTextbox(), 'Hello');
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    const errorNote = await screen.findByRole('note', {
      name: 'Chat request error',
    });
    expect(errorNote).toHaveTextContent('Model gateway unavailable');
    expect(
      within(
        screen.getByRole('region', { name: 'Conversation surface' })
      ).getByRole('note', { name: 'Chat request error' })
    ).toBe(errorNote);
  });

  it('clears the live trace status when a chat stream finishes without a final trace', async () => {
    const user = userEvent.setup();
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementationOnce(
      async ({ onEvent }) => {
        onEvent('assistant_message_started', {
          message_id: 'msg-stream',
          session_id: 'session-1',
        });
        onEvent('trace_status', {
          message_id: 'msg-stream',
          status: 'Finalizing response...',
        });
        onEvent('answer_delta', {
          message_id: 'msg-stream',
          delta: 'Streamed hello.',
        });
        onEvent('done', { message_id: 'msg-stream', session_id: 'session-1' });
      }
    );

    render(<ChatPage />, { wrapper: ChatPageTestWrapper });

    await user.type(getComposerTextbox(), 'Hello');
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    expect(await screen.findByText('Streamed hello.')).toBeInTheDocument();
    await waitFor(() => {
      expect(
        screen.queryByText('Finalizing response...')
      ).not.toBeInTheDocument();
    });
    expect(sendLlmChatWithUnifiedTools).not.toHaveBeenCalled();
  });

  it('renders streamed Conversation Activity Steps before the assistant answer', async () => {
    const user = userEvent.setup();
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementationOnce(
      async ({ onEvent }) => {
        onEvent('assistant_message_started', {
          message_id: 'msg-stream',
          session_id: 'session-1',
        });
        onEvent('activity_step', {
          message_id: 'msg-stream',
          activity_step: {
            id: 'tool-web-search',
            kind: 'tool',
            title: 'Web Search',
            status: 'succeeded',
            summary: 'Tool completed.',
          },
        });
        onEvent('trace_delta', {
          message_id: 'msg-stream',
          trace_delta: {
            id: 'model-request',
            kind: 'timing',
            title: 'Model request',
            content: 'Model request: 820 ms.',
            status: 'succeeded',
          },
        });
        onEvent('answer_delta', {
          message_id: 'msg-stream',
          delta: 'Streamed hello.',
        });
        onEvent('done', { message_id: 'msg-stream', session_id: 'session-1' });
      }
    );

    render(<ChatPage />, { wrapper: ChatPageTestWrapper });

    await user.type(getComposerTextbox(), 'Hello');
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    const activity = await screen.findByText('Web Search');
    const answer = await screen.findByText('Streamed hello.');
    expect(
      activity.compareDocumentPosition(answer) &
        Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy();
    expect(screen.getByText('Tool completed.')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Show Activity' }));
    expect(
      screen.queryByText('Model request: 820 ms.')
    ).not.toBeInTheDocument();
  });

  it('uses the shared surface for Admin Conversations while preserving admin-only tools', async () => {
    const user = userEvent.setup();
    vi.mocked(isAdminAuthenticated).mockReturnValue(true);
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementationOnce(
      async ({ onEvent }) => {
        onEvent('assistant_message_started', {
          message_id: 'msg-stream',
          session_id: 'session-1',
        });
        onEvent('activity_step', {
          message_id: 'msg-stream',
          activity_step: {
            id: 'tool-admin-config',
            kind: 'tool',
            title: 'Admin Config',
            status: 'succeeded',
            summary: 'Tool completed.',
          },
        });
        onEvent('trace_delta', {
          message_id: 'msg-stream',
          trace_delta: {
            id: 'model-request',
            kind: 'timing',
            title: 'Model request',
            content: 'Model request: 820 ms.',
            status: 'succeeded',
          },
        });
        onEvent('answer_delta', {
          message_id: 'msg-stream',
          delta: 'Admin answer.',
        });
        onEvent('done', { message_id: 'msg-stream', session_id: 'session-1' });
      }
    );

    render(<ChatPage />, { wrapper: ChatPageTestWrapper });

    await waitForValidatedAdminConfig();
    expect(
      screen.getByRole('region', { name: 'Conversation surface' })
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /Docs/ })
    ).not.toBeInTheDocument();

    await user.type(getComposerTextbox(), 'Check config');
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    expect(await screen.findByText('Admin Config')).toBeInTheDocument();
    expect(await screen.findByText('Admin answer.')).toBeInTheDocument();
    expect(screen.getByText('Model request: 820 ms.')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Hide Activity' })
    ).toHaveAttribute('aria-expanded', 'true');
  });

  it('surfaces stream errors without retrying after answer text has started', async () => {
    const user = userEvent.setup();
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementationOnce(
      async ({ onEvent }) => {
        onEvent('assistant_message_started', {
          message_id: 'msg-stream',
          session_id: 'session-1',
        });
        onEvent('answer_delta', {
          message_id: 'msg-stream',
          delta: 'Partial answer.',
        });
        onEvent('error', {
          message_id: 'msg-stream',
          detail: 'Model stream interrupted',
        });
      }
    );

    render(<ChatPage />, { wrapper: ChatPageTestWrapper });

    await user.type(getComposerTextbox(), 'Hello');
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    expect(await screen.findByText('Partial answer.')).toBeInTheDocument();
    const errorNote = await screen.findByRole('note', {
      name: 'Chat request error',
    });
    expect(errorNote).toHaveTextContent('Model stream interrupted');
    expect(sendLlmChatWithUnifiedTools).not.toHaveBeenCalled();
  });

  it('starts authenticated admin chat turns with no tools selected', async () => {
    const user = userEvent.setup();
    mockIsAdminAuthenticated.mockReturnValue(true);
    mockAdminFetch.mockImplementation((endpoint: string) => {
      if (endpoint === '/admin/deployment/config') {
        return Promise.resolve(
          Response.json({
            llm: [],
            embedding: [],
            email: [],
            storage: [],
            security: [],
            search: [],
            domains: [],
            ssl: [],
            general: [],
          })
        );
      }
      return Promise.resolve(Response.json({}));
    });
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementationOnce(
      async ({ onEvent }) => {
        onEvent('assistant_message_started', {
          message_id: 'admin-msg',
          session_id: 'session-1',
        });
        onEvent('answer_delta', {
          message_id: 'admin-msg',
          delta: 'I can inspect config.',
        });
        onEvent('done', { message_id: 'admin-msg', session_id: 'session-1' });
      }
    );

    render(<ChatPage />, { wrapper: ChatPageTestWrapper });

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Config' })).toHaveAttribute(
        'aria-pressed',
        'false'
      );
    });

    await user.type(getComposerTextbox(), 'Review instance config.');
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    await waitFor(() => {
      expect(sendLlmChatStreamWithUnifiedTools).toHaveBeenCalled();
    });
    expect(
      vi.mocked(sendLlmChatStreamWithUnifiedTools).mock.calls[0][0]
    ).toEqual(
      expect.objectContaining({
        content: 'Review instance config.',
        tools: [],
      })
    );
    expect(
      screen.queryByRole('button', { name: 'Approve changes' })
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Config' }));
    expect(
      screen.queryByRole('button', { name: 'Approve changes' })
    ).not.toBeInTheDocument();
  });

  it('sends conversational confirmation to Sage and announces direct-write refresh areas', async () => {
    const user = userEvent.setup();
    const refreshEvents: CustomEvent[] = [];
    const recordRefresh = (event: Event) =>
      refreshEvents.push(event as CustomEvent);
    window.addEventListener(ADMIN_CONFIG_CHANGED_EVENT, recordRefresh);
    try {
      mockIsAdminAuthenticated.mockReturnValue(true);
      vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementationOnce(
        async ({ onEvent }) => {
          onEvent('assistant_message_started', {
            message_id: 'admin-msg',
            session_id: 'session-1',
          });
          onEvent('answer_delta', {
            message_id: 'admin-msg',
            delta: 'Done.',
          });
          onEvent('done', {
            message_id: 'admin-msg',
            session_id: 'session-1',
            admin_config_affected_areas: ['instance_settings'],
          });
        }
      );

      render(<ChatPage />, { wrapper: ChatPageTestWrapper });
      await waitForValidatedAdminConfig();
      await user.type(getComposerTextbox(), 'apply it');
      await user.click(screen.getByRole('button', { name: 'Send message' }));

      await waitFor(() =>
        expect(sendLlmChatStreamWithUnifiedTools).toHaveBeenCalledOnce()
      );
      expect(
        vi.mocked(sendLlmChatStreamWithUnifiedTools).mock.calls[0][0]
      ).toEqual(
        expect.objectContaining({
          content: 'apply it',
          tools: expect.arrayContaining(['admin-config']),
        })
      );
      expect(refreshEvents[refreshEvents.length - 1]?.detail).toEqual({
        areas: ['instance_settings'],
      });
      expect(
        screen.queryByRole('button', { name: 'Approve changes' })
      ).not.toBeInTheDocument();
    } finally {
      window.removeEventListener(ADMIN_CONFIG_CHANGED_EVENT, recordRefresh);
    }
  });

  it('does not expose admin-only tools when a stale admin marker fails server validation', async () => {
    const user = userEvent.setup();
    mockIsAdminAuthenticated.mockReturnValue(true);
    mockValidateAdminSession.mockResolvedValue('unauthenticated');
    localStorage.setItem('enclave_admin_pubkey', 'stale-admin-pubkey');
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementationOnce(
      async ({ onEvent }) => {
        onEvent('assistant_message_started', {
          message_id: 'user-msg',
          session_id: 'session-1',
        });
        onEvent('answer_delta', {
          message_id: 'user-msg',
          delta: 'User answer.',
        });
        onEvent('done', { message_id: 'user-msg', session_id: 'session-1' });
      }
    );

    render(<ChatPage />, { wrapper: ChatPageTestWrapper });

    await waitFor(() => {
      expect(mockValidateAdminSession).toHaveBeenCalled();
    });
    await waitFor(() => {
      expect(
        screen.queryByRole('button', { name: 'Config' })
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole('button', { name: 'Database' })
      ).not.toBeInTheDocument();
    });

    expect(
      screen.queryByRole('button', { name: 'Web' })
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Resources' })
    ).not.toBeInTheDocument();

    await user.type(getComposerTextbox(), 'I need user help.');
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    await waitFor(() => {
      expect(sendLlmChatStreamWithUnifiedTools).toHaveBeenCalled();
    });
    const callArgs = vi.mocked(sendLlmChatStreamWithUnifiedTools).mock
      .calls[0][0];
    expect(callArgs.tools).toContain('curated-resources');
    expect(callArgs.tools).not.toContain('admin-config');
    expect(callArgs.tools).not.toContain('db-query');
  });

  it('falls back to user mode when admin session validation hangs', async () => {
    vi.useFakeTimers();
    mockIsAdminAuthenticated.mockReturnValue(true);
    mockValidateAdminSession.mockReturnValue(new Promise(() => {}));
    localStorage.setItem('enclave_admin_pubkey', 'stale-admin-pubkey');

    render(<ChatPage />, { wrapper: ChatPageTestWrapper });

    expect(mockValidateAdminSession).toHaveBeenCalled();

    await act(async () => {
      await Promise.resolve();
      await vi.advanceTimersByTimeAsync(8000);
      await Promise.resolve();
    });

    expect(
      screen.queryByRole('button', { name: 'Config' })
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Resources' })
    ).not.toBeInTheDocument();
  });

  it('admin tools default off without inherited web-search', async () => {
    const user = userEvent.setup();
    mockIsAdminAuthenticated.mockReturnValue(true);
    mockAdminFetch.mockImplementation((endpoint: string) => {
      if (endpoint === '/admin/deployment/config') {
        return Promise.resolve(
          Response.json({
            llm: [],
            embedding: [],
            email: [],
            storage: [],
            security: [],
            search: [],
            domains: [],
            ssl: [],
            general: [],
          })
        );
      }
      return Promise.resolve(Response.json({}));
    });
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementationOnce(
      async ({ onEvent }) => {
        onEvent('assistant_message_started', {
          message_id: 'admin-msg',
          session_id: 'session-1',
        });
        onEvent('answer_delta', {
          message_id: 'admin-msg',
          delta: 'SMTP is configured.',
        });
        onEvent('done', { message_id: 'admin-msg', session_id: 'session-1' });
      }
    );

    render(<ChatPage />, { wrapper: ChatPageTestWrapper });

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Config' })).toHaveAttribute(
        'aria-pressed',
        'false'
      );
    });

    expect(screen.getByRole('button', { name: 'Web' })).toHaveAttribute(
      'aria-pressed',
      'false'
    );

    await user.type(getComposerTextbox(), 'What is the SMTP host?');
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    await waitFor(() => {
      expect(sendLlmChatStreamWithUnifiedTools).toHaveBeenCalled();
    });

    const callArgs = vi.mocked(sendLlmChatStreamWithUnifiedTools).mock
      .calls[0][0];
    expect(callArgs.tools).toEqual([]);
  });

  it('clears admin secret sharing when starting fresh or disabling Config', async () => {
    const user = userEvent.setup();
    mockIsAdminAuthenticated.mockReturnValue(true);

    render(<ChatPage />, { wrapper: ChatPageTestWrapper });

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Config' })).toHaveAttribute(
        'aria-pressed',
        'false'
      );
    });
    await user.click(screen.getByRole('button', { name: 'Config' }));

    const shareToggle = screen.getByLabelText(
      'Share secret env vars'
    ) as HTMLInputElement;
    await user.click(shareToggle);
    expect(shareToggle).toBeChecked();

    await user.click(screen.getByRole('button', { name: 'Start a new chat' }));
    expect(screen.getByLabelText('Share secret env vars')).not.toBeChecked();

    await user.click(screen.getByLabelText('Share secret env vars'));
    expect(screen.getByLabelText('Share secret env vars')).toBeChecked();

    await user.click(screen.getByRole('button', { name: 'Config' }));
    expect(
      screen.queryByLabelText('Share secret env vars')
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Config' }));
    expect(screen.getByLabelText('Share secret env vars')).not.toBeChecked();
  });

  it('preserves admin secret sharing on first persisted session assignment', async () => {
    const user = userEvent.setup();
    mockIsAdminAuthenticated.mockReturnValue(true);
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementationOnce(
      async ({ onEvent }) => {
        onEvent('assistant_message_started', {
          message_id: 'admin-msg',
          session_id: 'session-1',
        });
        onEvent('answer_delta', {
          message_id: 'admin-msg',
          delta: 'Admin answer.',
        });
        onEvent('done', { message_id: 'admin-msg', session_id: 'session-1' });
      }
    );

    render(<ChatPage />, { wrapper: ChatPageTestWrapper });

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Config' })).toHaveAttribute(
        'aria-pressed',
        'false'
      );
    });
    await user.click(screen.getByRole('button', { name: 'Config' }));

    await user.click(screen.getByLabelText('Share secret env vars'));
    expect(screen.getByLabelText('Share secret env vars')).toBeChecked();

    await user.type(getComposerTextbox(), 'Review instance config.');
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    expect(await screen.findByText('Admin answer.')).toBeInTheDocument();
    expect(screen.getByLabelText('Share secret env vars')).toBeChecked();
  });

  it('surfaces safe provider context limit errors in admin chat without non-streaming fallback', async () => {
    const user = userEvent.setup();
    mockIsAdminAuthenticated.mockReturnValue(true);
    mockAdminFetch.mockImplementation((endpoint: string) => {
      if (endpoint === '/admin/deployment/config') {
        return Promise.resolve(
          Response.json({
            llm: [],
            embedding: [],
            email: [],
            storage: [],
            security: [],
            search: [],
            domains: [],
            ssl: [],
            general: [],
          })
        );
      }
      return Promise.resolve(Response.json({}));
    });
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementationOnce(
      async ({ onEvent }) => {
        onEvent('assistant_message_started', {
          message_id: 'admin-msg',
          session_id: 'session-1',
        });
        onEvent('error', {
          message_id: 'admin-msg',
          detail:
            'Token limit exceeded for this session. Please start a new session.',
        });
      }
    );

    render(<ChatPage />, { wrapper: ChatPageTestWrapper });

    await waitForValidatedAdminConfig();

    await user.type(getComposerTextbox(), 'Continue this long session.');
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    const errorNote = await screen.findByRole('note', {
      name: 'Chat request error',
    });
    expect(errorNote).toHaveTextContent(
      'Token limit exceeded for this session. Start a new assistant conversation to continue.'
    );
    expect(sendLlmChatWithUnifiedTools).not.toHaveBeenCalled();
  });

  it('offers a fresh assistant conversation recovery action after admin context limit failures', async () => {
    const user = userEvent.setup();
    mockIsAdminAuthenticated.mockReturnValue(true);
    mockAdminFetch.mockImplementation((endpoint: string) => {
      if (endpoint === '/admin/deployment/config') {
        return Promise.resolve(
          Response.json({
            llm: [],
            embedding: [],
            email: [],
            storage: [],
            security: [],
            search: [],
            domains: [],
            ssl: [],
            general: [],
          })
        );
      }
      return Promise.resolve(Response.json({}));
    });

    let streamCalls = 0;
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementation(
      async ({ sessionId, onEvent }) => {
        streamCalls += 1;
        if (streamCalls === 1) {
          onEvent('assistant_message_started', {
            message_id: 'admin-msg',
            session_id: 'session-1',
          });
          onEvent('error', {
            message_id: 'admin-msg',
            detail:
              'Token limit exceeded for this session. Please start a new session.',
          });
          return;
        }

        expect(sessionId).toBeNull();
        onEvent('assistant_message_started', {
          message_id: 'admin-msg-2',
          session_id: 'session-2',
        });
        onEvent('done', { message_id: 'admin-msg-2', session_id: 'session-2' });
      }
    );

    render(<ChatPage />, { wrapper: ChatPageTestWrapper });

    await waitForValidatedAdminConfig();

    await user.type(getComposerTextbox(), 'Continue this long session.');
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    await user.click(
      await screen.findByRole('button', {
        name: 'Start new assistant conversation',
      })
    );

    await user.type(getComposerTextbox(), 'Try again after reset.');
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    await waitFor(() => {
      expect(sendLlmChatStreamWithUnifiedTools).toHaveBeenCalledTimes(2);
    });
  });

  it('does not replay an admin Config turn after an ambiguous stream failure', async () => {
    const user = userEvent.setup();
    mockIsAdminAuthenticated.mockReturnValue(true);
    mockAdminFetch.mockImplementation((endpoint: string) => {
      if (endpoint === '/admin/deployment/config') {
        return Promise.resolve(
          Response.json({
            llm: [],
            embedding: [],
            email: [],
            storage: [],
            security: [],
            search: [],
            domains: [],
            ssl: [],
            general: [],
          })
        );
      }
      return Promise.resolve(Response.json({}));
    });
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockRejectedValueOnce(
      new Error('Stream transport failed')
    );
    render(<ChatPage />, { wrapper: ChatPageTestWrapper });

    await waitForValidatedAdminConfig();

    await user.type(getComposerTextbox(), 'Check model availability.');
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    const errorNote = await screen.findByRole('note', {
      name: 'Chat request error',
    });
    expect(errorNote).toHaveTextContent('Stream transport failed');
    expect(sendLlmChatWithUnifiedTools).not.toHaveBeenCalled();
  });

  it('sends only the new message on admin chat Config turns without context notices', async () => {
    const user = userEvent.setup();
    mockIsAdminAuthenticated.mockReturnValue(true);
    mockAdminFetch.mockImplementation((endpoint: string) => {
      if (endpoint === '/admin/deployment/config') {
        return Promise.resolve(
          Response.json({
            llm: [],
            embedding: [],
            email: [],
            storage: [],
            security: [],
            search: [],
            domains: [],
            ssl: [],
            general: [],
          })
        );
      }
      return Promise.resolve(Response.json({}));
    });
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementation(
      async ({ onEvent }) => {
        onEvent('assistant_message_started', {
          message_id: `msg-${Math.random()}`,
          session_id: 'session-1',
        });
        onEvent('answer_delta', {
          message_id: 'msg-assistant',
          delta: 'Acknowledged.',
        });
        onEvent('done', {
          message_id: 'msg-assistant',
          session_id: 'session-1',
        });
      }
    );

    render(<ChatPage />, { wrapper: ChatPageTestWrapper });

    await waitForValidatedAdminConfig();

    fireEvent.change(getComposerTextbox(), {
      target: {
        value: `Summarize this admin context ${'SAFE_PADDING '.repeat(220)}`,
      },
    });
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    await waitFor(() => {
      expect(sendLlmChatStreamWithUnifiedTools).toHaveBeenCalledTimes(1);
    });

    fireEvent.change(getComposerTextbox(), {
      target: { value: 'Continue with config advice.' },
    });
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    await waitFor(() => {
      expect(sendLlmChatStreamWithUnifiedTools).toHaveBeenCalledTimes(2);
    });
    // Sage owns the Conversation history; the browser sends only the new message.
    const streamCalls = vi.mocked(sendLlmChatStreamWithUnifiedTools).mock.calls;
    for (const [request] of streamCalls) {
      expect(request).not.toHaveProperty('conversationHistory');
    }
    expect(streamCalls[1][0]).toEqual(
      expect.objectContaining({
        content: 'Continue with config advice.',
        sessionId: 'session-1',
      })
    );
    expect(
      screen.queryByText(/context was (reduced|summarized)/i)
    ).not.toBeInTheDocument();
  });
});
