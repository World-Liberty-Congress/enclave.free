import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  AdminConfigAssistant,
  ONBOARDING_WELCOME_MESSAGE,
} from './AdminConfigAssistant';
import { adminFetch } from '../../utils/adminApi';
import { ADMIN_CONFIG_CHANGED_EVENT } from '../../utils/adminConfigEvents';
import {
  sendLlmChatStreamWithUnifiedTools,
  sendLlmChatWithUnifiedTools,
  StreamTurnError,
} from '../../utils/llmChat';
import { ThemeProvider } from '../../theme';

vi.mock('../../utils/adminApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../utils/adminApi')>();
  return {
    ...actual,
    adminFetch: vi.fn(),
  };
});

vi.mock('../../utils/llmChat', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../utils/llmChat')>()),
  sendLlmChatStreamWithUnifiedTools: vi.fn(),
  sendLlmChatWithUnifiedTools: vi.fn(),
}));

import {
  registerAdminResilienceInstrumentationListener,
  resetAdminResilienceInstrumentationListeners,
  type AdminResilienceInstrumentationEvent,
} from '../../utils/adminResilienceInstrumentation';

describe('AdminConfigAssistant', () => {
  const mockAdminFetch = vi.mocked(adminFetch);

  async function enableConfigTool(user: ReturnType<typeof userEvent.setup>) {
    const configButton = screen.getByRole('button', { name: 'Config' });
    if (configButton.getAttribute('aria-pressed') !== 'true') {
      await user.click(configButton);
    }
    expect(configButton).toHaveAttribute('aria-pressed', 'true');
  }

  beforeEach(() => {
    resetAdminResilienceInstrumentationListeners();
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
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith('/session-defaults')) {
          return Promise.resolve(Response.json({ web_search_enabled: false }));
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
    mockAdminFetch.mockImplementation((endpoint: string) => {
      if (endpoint === '/admin/settings') {
        return Promise.resolve(
          Response.json({ settings: { instance_name: 'Enclave' } })
        );
      }
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
            general: [
              {
                key: 'LLM_API_KEY',
                value: '[CONFIGURED]',
                is_secret: true,
                requires_restart: true,
                description: 'Model Provider API key',
              },
            ],
          })
        );
      }
      if (endpoint === '/admin/deployment/config/LLM_API_KEY/reveal') {
        return Promise.resolve(
          Response.json({ key: 'LLM_API_KEY', value: 'super-secret-token' })
        );
      }
      if (endpoint === '/admin/ai-config') {
        return Promise.resolve(
          Response.json({ prompt_sections: [], parameters: [], defaults: [] })
        );
      }
      if (endpoint === '/admin/user-types') {
        return Promise.resolve(Response.json({ types: [] }));
      }
      if (endpoint === '/ingest/admin/documents/defaults') {
        return Promise.resolve(Response.json({ documents: [] }));
      }
      if (endpoint === '/admin/deployment/health') {
        return Promise.resolve(Response.json({ ok: true }));
      }
      return Promise.resolve(Response.json({}));
    });
  });

  afterEach(() => {
    resetAdminResilienceInstrumentationListeners();
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it('shows the full admin assistant tool set in the sidebar', () => {
    render(
      <ThemeProvider>
        <AdminConfigAssistant />
      </ThemeProvider>
    );

    expect(screen.getByRole('button', { name: 'Knowledge' })).toHaveAttribute(
      'aria-pressed',
      'false'
    );
    expect(screen.getByRole('button', { name: 'Resources' })).toHaveAttribute(
      'aria-pressed',
      'false'
    );
    expect(screen.getByRole('button', { name: 'Web' })).toHaveAttribute(
      'aria-pressed',
      'false'
    );
    expect(screen.getByRole('button', { name: 'Config' })).toHaveAttribute(
      'aria-pressed',
      'true'
    );
    expect(screen.getByRole('button', { name: 'Config' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Database' })).toHaveAttribute(
      'aria-pressed',
      'false'
    );
  });

  it('sends Knowledge Search when an admin enables it in the sidebar', async () => {
    const user = userEvent.setup();
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementationOnce(
      async ({ onEvent }) => {
        onEvent('assistant_message_started', {
          message_id: 'msg-1',
          session_id: 'session-1',
        });
        onEvent('done', { message_id: 'msg-1', session_id: 'session-1' });
      }
    );

    render(
      <ThemeProvider>
        <AdminConfigAssistant />
      </ThemeProvider>
    );

    await user.click(screen.getByRole('button', { name: 'Knowledge' }));
    await user.type(
      screen.getByRole('textbox', { name: 'Ask about admin configuration...' }),
      'Use the uploaded guide.'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    await waitFor(() => {
      expect(sendLlmChatStreamWithUnifiedTools).toHaveBeenCalledOnce();
    });

    const callArgs = vi.mocked(sendLlmChatStreamWithUnifiedTools).mock
      .calls[0][0];
    expect(callArgs.tools).toEqual(
      expect.arrayContaining(['knowledge-search'])
    );
    expect(callArgs.tools).toContain('admin-config');
  });

  it('opts Database-enabled turns into Admin signer-decrypted context', async () => {
    const user = userEvent.setup();
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementationOnce(
      async ({ onEvent }) => {
        onEvent('assistant_message_started', {
          message_id: 'msg-1',
          session_id: 'session-1',
        });
        onEvent('done', { message_id: 'msg-1', session_id: 'session-1' });
      }
    );

    render(
      <ThemeProvider>
        <AdminConfigAssistant />
      </ThemeProvider>
    );

    await user.click(screen.getByRole('button', { name: 'Database' }));
    await user.type(
      screen.getByRole('textbox', { name: 'Ask about admin configuration...' }),
      'Tell me about the users in our db.'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    await waitFor(() => {
      expect(sendLlmChatStreamWithUnifiedTools).toHaveBeenCalledOnce();
    });

    expect(
      vi.mocked(sendLlmChatStreamWithUnifiedTools).mock.calls[0][0]
    ).toEqual(
      expect.objectContaining({
        tools: expect.arrayContaining(['db-query']),
        adminUserLookup: true,
      })
    );
  });

  it('does not expose broad tool toggles during onboarding setup', () => {
    render(
      <ThemeProvider>
        <AdminConfigAssistant purpose="onboarding" />
      </ThemeProvider>
    );

    expect(
      screen.queryByRole('button', { name: 'Config' })
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Knowledge' })
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Resources' })
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Web' })
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Database' })
    ).not.toBeInTheDocument();
  });

  it('keeps the guided question numbers aligned with the Sage onboarding contract', () => {
    expect(
      Array.from(
        ONBOARDING_WELCOME_MESSAGE.matchAll(/\d+\. \*\*([^*]+)\*\*/g),
        (match) => match[1]
      )
    ).toEqual([
      'Name',
      'Description',
      'Assistant name',
      'Accent color',
      'Theme',
      'Default language',
      'Tagline',
      'New users',
      'User types',
    ]);
  });

  it('marks guided setup turns as admin onboarding for Sage', async () => {
    const user = userEvent.setup();
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementationOnce(
      async ({ onEvent }) => {
        onEvent('assistant_message_started', {
          message_id: 'msg-1',
          session_id: 'session-1',
        });
        onEvent('done', { message_id: 'msg-1', session_id: 'session-1' });
      }
    );

    render(
      <ThemeProvider>
        <AdminConfigAssistant purpose="onboarding" />
      </ThemeProvider>
    );

    await user.type(
      screen.getByRole('textbox', { name: 'Ask about admin configuration...' }),
      '1. FreeThem, 4. blue, 8. approve each person'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    await waitFor(() => {
      expect(sendLlmChatStreamWithUnifiedTools).toHaveBeenCalledOnce();
    });
    expect(
      vi.mocked(sendLlmChatStreamWithUnifiedTools).mock.calls[0][0]
    ).toEqual(
      expect.objectContaining({
        content: '1. FreeThem, 4. blue, 8. approve each person',
        conversationSurface: 'admin-onboarding',
        tools: ['admin-config'],
      })
    );
  });

  it('continues follow-up turns in the Sage session without resending earlier turns', async () => {
    const user = userEvent.setup();
    vi.mocked(sendLlmChatStreamWithUnifiedTools)
      .mockImplementationOnce(async ({ onEvent }) => {
        onEvent('assistant_message_started', {
          message_id: 'msg-1',
          session_id: 'session-1',
        });
        onEvent('answer_delta', {
          message_id: 'msg-1',
          delta: 'I recommend updating Instance Name and Assistant Name.',
        });
        onEvent('done', { message_id: 'msg-1', session_id: 'session-1' });
      })
      .mockImplementationOnce(async ({ onEvent }) => {
        onEvent('assistant_message_started', {
          message_id: 'msg-2',
          session_id: 'session-1',
        });
        onEvent('answer_delta', {
          message_id: 'msg-2',
          delta: 'Applying those suggestions.',
        });
        onEvent('done', { message_id: 'msg-2', session_id: 'session-1' });
      });

    render(
      <ThemeProvider>
        <AdminConfigAssistant />
      </ThemeProvider>
    );
    await enableConfigTool(user);

    await user.type(
      screen.getByRole('textbox', { name: 'Ask about admin configuration...' }),
      'Change more of the copy.'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    expect(
      await screen.findByText(
        'I recommend updating Instance Name and Assistant Name.'
      )
    ).toBeInTheDocument();

    await user.type(
      screen.getByRole('textbox', { name: 'Ask about admin configuration...' }),
      'your suggestions above'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    await waitFor(() => {
      expect(sendLlmChatStreamWithUnifiedTools).toHaveBeenCalledTimes(2);
    });
    // Sage owns the Conversation history; the browser sends only the new message.
    const followUpRequest = vi.mocked(sendLlmChatStreamWithUnifiedTools).mock
      .calls[1][0];
    expect(followUpRequest).toEqual(
      expect.objectContaining({
        content: 'your suggestions above',
        sessionId: 'session-1',
      })
    );
    expect(followUpRequest).not.toHaveProperty('conversationHistory');
    expect(screen.queryByRole('note')).not.toBeInTheDocument();
  });

  it('renders live Trace Deltas from the admin assistant stream', async () => {
    const user = userEvent.setup();
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementationOnce(
      async ({ onEvent }) => {
        onEvent('assistant_message_started', {
          message_id: 'msg-1',
          session_id: 'session-1',
        });
        onEvent('trace_delta', {
          message_id: 'msg-1',
          trace_delta: {
            id: 'tool-call-admin-config-1',
            kind: 'tool_call',
            title: 'Admin Config',
            tool_name: 'read_instance_settings',
            status: 'running',
            content: 'Reading instance settings.',
          },
        });
        onEvent('trace_delta', {
          message_id: 'msg-1',
          trace_delta: {
            id: 'model-request',
            kind: 'timing',
            title: 'Model request',
            content: 'Model request: 820 ms.',
            status: 'succeeded',
          },
        });
        onEvent('answer_delta', {
          message_id: 'msg-1',
          delta: 'I checked the settings.',
        });
        onEvent('done', { message_id: 'msg-1', session_id: 'session-1' });
      }
    );

    render(
      <ThemeProvider>
        <AdminConfigAssistant />
      </ThemeProvider>
    );
    await enableConfigTool(user);

    await user.type(
      screen.getByRole('textbox', { name: 'Ask about admin configuration...' }),
      'Check the current instance settings.'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    expect(await screen.findByText('Admin Config')).toBeInTheDocument();
    expect(screen.getByText('read_instance_settings')).toBeInTheDocument();
    expect(screen.getByText('Reading instance settings.')).toBeInTheDocument();
    expect(screen.getByText('Model request: 820 ms.')).toBeInTheDocument();
    expect(screen.getByText('I checked the settings.')).toBeInTheDocument();
  });

  it('merges repeated Trace Delta updates without dropping previous fields', async () => {
    const user = userEvent.setup();
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementationOnce(
      async ({ onEvent }) => {
        onEvent('assistant_message_started', {
          message_id: 'msg-1',
          session_id: 'session-1',
        });
        onEvent('trace_delta', {
          message_id: 'msg-1',
          trace_delta: {
            id: 'tool-call-admin-config-1',
            kind: 'tool_call',
            title: 'Admin Config',
            tool_name: 'read_instance_settings',
            status: 'running',
            content: 'Reading instance settings.',
          },
        });
        onEvent('trace_delta', {
          message_id: 'msg-1',
          trace_delta: {
            id: 'tool-call-admin-config-1',
            kind: 'tool_call',
            status: 'succeeded',
          },
        });
        onEvent('answer_delta', {
          message_id: 'msg-1',
          delta: 'I checked the settings.',
        });
        onEvent('done', { message_id: 'msg-1', session_id: 'session-1' });
      }
    );

    render(
      <ThemeProvider>
        <AdminConfigAssistant />
      </ThemeProvider>
    );
    await enableConfigTool(user);

    await user.type(
      screen.getByRole('textbox', { name: 'Ask about admin configuration...' }),
      'Check the current instance settings.'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    expect(await screen.findByText('Admin Config')).toBeInTheDocument();
    expect(screen.getByText('read_instance_settings')).toBeInTheDocument();
    expect(screen.getByText('Reading instance settings.')).toBeInTheDocument();
    expect(screen.getByText('succeeded')).toBeInTheDocument();
    expect(screen.getAllByText('Admin Config')).toHaveLength(1);
  });

  it('renders live Trace Deltas during onboarding before the final answer completes', async () => {
    const user = userEvent.setup();
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementationOnce(
      async ({ onEvent }) => {
        onEvent('assistant_message_started', {
          message_id: 'msg-1',
          session_id: 'session-1',
        });
        onEvent('trace_delta', {
          message_id: 'msg-1',
          trace_delta: {
            id: 'tool-call-admin-config-onboarding',
            kind: 'tool_call',
            title: 'Admin Config',
            tool_name: 'read_onboarding_status',
            status: 'running',
            content: 'Checking onboarding status.',
          },
        });
        onEvent('answer_delta', {
          message_id: 'msg-1',
          delta: 'I prepared the setup draft.',
        });
        onEvent('done', { message_id: 'msg-1', session_id: 'session-1' });
      }
    );

    render(
      <ThemeProvider>
        <AdminConfigAssistant purpose="onboarding" />
      </ThemeProvider>
    );

    expect(
      screen.getByText(/Welcome — let's set up your space/)
    ).toBeInTheDocument();

    await user.type(
      screen.getByRole('textbox', { name: 'Ask about admin configuration...' }),
      '1. FreeThem, 5. dark, 8. let them in right away'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    expect(await screen.findByText('Admin Config')).toBeInTheDocument();
    expect(screen.getByText('read_onboarding_status')).toBeInTheDocument();
    expect(screen.getByText('Checking onboarding status.')).toBeInTheDocument();
    expect(screen.getByText('I prepared the setup draft.')).toBeInTheDocument();
  });

  it('always sends the dedicated Config Tool', async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith('/session-defaults')) {
          return Promise.resolve(Response.json({ web_search_enabled: true }));
        }
        return Promise.resolve(Response.json({}));
      })
    );
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementationOnce(
      async ({ onEvent }) => {
        onEvent('assistant_message_started', {
          message_id: 'msg-1',
          session_id: 'session-1',
        });
        onEvent('done', { message_id: 'msg-1', session_id: 'session-1' });
      }
    );

    render(
      <ThemeProvider>
        <AdminConfigAssistant />
      </ThemeProvider>
    );

    expect(screen.getByRole('button', { name: 'Config' })).toHaveAttribute(
      'aria-pressed',
      'true'
    );

    await user.type(
      screen.getByRole('textbox', { name: 'Ask about admin configuration...' }),
      'What is the SMTP host?'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    await waitFor(() => {
      expect(sendLlmChatStreamWithUnifiedTools).toHaveBeenCalled();
    });

    const callArgs = vi.mocked(sendLlmChatStreamWithUnifiedTools).mock
      .calls[0][0];
    expect(callArgs.tools).toEqual(['admin-config']);
    expect(screen.queryByText(/Pending changes:/)).not.toBeInTheDocument();

    expect(screen.getByRole('button', { name: 'Config' })).toBeDisabled();
    expect(screen.queryByText(/Pending changes:/)).not.toBeInTheDocument();
  });

  it('relies on Sage for document context retrieval (Sage-only path)', async () => {
    const user = userEvent.setup();
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementationOnce(
      async ({ onEvent }) => {
        onEvent('assistant_message_started', {
          message_id: 'msg-1',
          session_id: 'session-1',
        });
        onEvent('answer_delta', {
          message_id: 'msg-1',
          delta: 'I can update the instance settings.',
        });
        onEvent('done', { message_id: 'msg-1', session_id: 'session-1' });
      }
    );

    render(
      <ThemeProvider>
        <AdminConfigAssistant />
      </ThemeProvider>
    );

    expect(screen.getByRole('button', { name: 'Config' })).toHaveAttribute(
      'aria-pressed',
      'true'
    );

    await user.type(
      screen.getByRole('textbox', { name: 'Ask about admin configuration...' }),
      'Set up the theme from the uploaded guide.'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    await waitFor(() => {
      expect(sendLlmChatStreamWithUnifiedTools).toHaveBeenCalled();
    });
    expect(
      vi.mocked(sendLlmChatStreamWithUnifiedTools).mock.calls[0][0]
    ).toEqual(
      expect.objectContaining({
        content: 'Set up the theme from the uploaded guide.',
        tools: expect.arrayContaining(['admin-config']),
      })
    );
    expect(mockAdminFetch).not.toHaveBeenCalledWith(
      '/admin/deployment/config',
      undefined
    );
    expect(mockAdminFetch).not.toHaveBeenCalledWith(
      '/admin/ai-config',
      undefined
    );
  });

  it('skips client-side document context fetching (Sage-only path)', async () => {
    const user = userEvent.setup();

    mockAdminFetch.mockImplementation((endpoint: string) => {
      if (endpoint === '/admin/deployment/health') {
        return Promise.resolve(Response.json({ ok: true }));
      }
      return Promise.resolve(Response.json({}));
    });

    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementationOnce(
      async ({ onEvent }) => {
        onEvent('assistant_message_started', {
          message_id: 'msg-1',
          session_id: 'session-1',
        });
        onEvent('done', { message_id: 'msg-1', session_id: 'session-1' });
      }
    );

    render(
      <ThemeProvider>
        <AdminConfigAssistant />
      </ThemeProvider>
    );
    await enableConfigTool(user);

    await user.type(
      screen.getByRole('textbox', { name: 'Ask about admin configuration...' }),
      'Set up the theme from the uploaded guide.'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    await waitFor(() => {
      expect(sendLlmChatStreamWithUnifiedTools).toHaveBeenCalled();
    });

    expect(mockAdminFetch).not.toHaveBeenCalled();
  });

  it('does not prefetch scoped deployment config or reveal secrets on send', async () => {
    const user = userEvent.setup();
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementationOnce(
      async ({ onEvent }) => {
        onEvent('assistant_message_started', {
          message_id: 'msg-1',
          session_id: 'session-1',
        });
        onEvent('answer_delta', {
          message_id: 'msg-1',
          delta: 'I can review the config safely.',
        });
        onEvent('done', { message_id: 'msg-1', session_id: 'session-1' });
      }
    );

    render(
      <ThemeProvider>
        <AdminConfigAssistant />
      </ThemeProvider>
    );
    await enableConfigTool(user);

    await user.type(
      screen.getByRole('textbox', { name: 'Ask about admin configuration...' }),
      'Review deployment config.'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    await waitFor(() => {
      expect(sendLlmChatStreamWithUnifiedTools).toHaveBeenCalled();
    });
    expect(mockAdminFetch).not.toHaveBeenCalledWith(
      '/admin/deployment/config/LLM_API_KEY/reveal',
      undefined
    );
    expect(mockAdminFetch).not.toHaveBeenCalledWith(
      '/admin/ai-config',
      undefined
    );
  });

  it('refreshes redaction metadata without loading prompt context', async () => {
    const user = userEvent.setup();

    render(
      <ThemeProvider>
        <AdminConfigAssistant />
      </ThemeProvider>
    );

    await user.click(screen.getByRole('button', { name: 'Refresh context' }));

    await waitFor(() => {
      expect(mockAdminFetch).toHaveBeenCalledWith(
        '/admin/deployment/config',
        undefined
      );
    });
  });

  it('surfaces early streamed provider errors without falling back to opaque HTTP errors', async () => {
    const user = userEvent.setup();
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementationOnce(
      async ({ onEvent }) => {
        onEvent('assistant_message_started', {
          message_id: 'msg-1',
          session_id: 'session-1',
        });
        onEvent('error', {
          message_id: 'msg-1',
          session_id: 'session-1',
          detail:
            'Token limit exceeded for this session. Please start a new session.',
        });
      }
    );

    render(
      <ThemeProvider>
        <AdminConfigAssistant />
      </ThemeProvider>
    );
    await enableConfigTool(user);

    await user.type(
      screen.getByRole('textbox', { name: 'Ask about admin configuration...' }),
      'Continue reviewing deployment config.'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    expect(
      await screen.findByText(
        'Token limit exceeded for this session. Start a new assistant conversation to continue.'
      )
    ).toBeInTheDocument();
    expect(sendLlmChatWithUnifiedTools).not.toHaveBeenCalled();
  });

  it('does not replay a Config write turn after an ambiguous stream failure', async () => {
    const user = userEvent.setup();
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockRejectedValueOnce(
      new Error('Stream transport failed')
    );

    render(
      <ThemeProvider>
        <AdminConfigAssistant />
      </ThemeProvider>
    );
    await enableConfigTool(user);

    await user.type(
      screen.getByRole('textbox', { name: 'Ask about admin configuration...' }),
      'Apply the confirmed description change.'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    expect(
      await screen.findByText('Stream transport failed')
    ).toBeInTheDocument();
    expect(sendLlmChatWithUnifiedTools).not.toHaveBeenCalled();
  });

  it.each(['stop', 'failure'])(
    'settles buffered sidebar output and lookup progress after %s without replay',
    async (ending) => {
      const user = userEvent.setup();
      let fail!: () => void;
      vi.spyOn(performance, 'now').mockReturnValue(1);
      vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementationOnce(
        ({ onEvent, signal }) => {
          onEvent('assistant_message_started', {
            message_id: 'pending-lookup',
            session_id: 'session-1',
          });
          onEvent('trace_status', { status: 'Searching Users: 20 scanned.' });
          onEvent('answer_delta', { delta: 'Buffered partial answer' });
          return new Promise((_resolve, reject) => {
            fail = () =>
              reject(new StreamTurnError('Stream interrupted', true));
            signal?.addEventListener('abort', fail, { once: true });
          });
        }
      );
      render(
        <ThemeProvider>
          <AdminConfigAssistant />
        </ThemeProvider>
      );
      await user.type(
        screen.getByRole('textbox', {
          name: 'Ask about admin configuration...',
        }),
        'Find users'
      );
      await user.click(screen.getByRole('button', { name: 'Send message' }));
      await screen.findByRole('button', { name: 'Stop response' });
      if (ending === 'stop')
        await user.click(screen.getByRole('button', { name: 'Stop response' }));
      else await act(async () => fail());
      expect(
        await screen.findByText('Buffered partial answer')
      ).toBeInTheDocument();
      await waitFor(() =>
        expect(
          screen.queryByText('Searching Users: 20 scanned.')
        ).not.toBeInTheDocument()
      );
      expect(
        screen.queryByRole('button', { name: 'Stop response' })
      ).not.toBeInTheDocument();
      expect(sendLlmChatWithUnifiedTools).not.toHaveBeenCalled();
      vi.restoreAllMocks();
    }
  );

  it('never replays a Database stream that failed before assistant text', async () => {
    const user = userEvent.setup();
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockRejectedValueOnce(
      Object.assign(new Error('Stream interrupted'), { streamStarted: true })
    );
    render(
      <ThemeProvider>
        <AdminConfigAssistant />
      </ThemeProvider>
    );
    await user.click(screen.getByRole('button', { name: 'Config' }));
    await user.click(screen.getByRole('button', { name: 'Database' }));
    await user.type(
      screen.getByRole('textbox', { name: 'Ask about admin configuration...' }),
      'Find a user'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));
    expect(await screen.findByText('Stream interrupted')).toBeInTheDocument();
    expect(sendLlmChatWithUnifiedTools).not.toHaveBeenCalled();
    expect(sendLlmChatStreamWithUnifiedTools).toHaveBeenCalledWith(
      expect.objectContaining({
        adminUserLookup: true,
        signal: expect.any(AbortSignal),
      })
    );
  });

  it('surfaces classified raw provider errors without falling back to non-streaming chat', async () => {
    const user = userEvent.setup();
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementationOnce(
      async ({ onEvent }) => {
        onEvent('assistant_message_started', {
          message_id: 'msg-1',
          session_id: 'session-1',
        });
        onEvent('error', {
          message_id: 'msg-1',
          session_id: 'session-1',
          detail:
            'HttpError: Invalid status code 429 Too Many Requests with message: {"error":{"code":"insufficient_quota","message":"Token limit exceeded for this session. Please start a new session."}}',
        });
      }
    );

    render(
      <ThemeProvider>
        <AdminConfigAssistant />
      </ThemeProvider>
    );
    await enableConfigTool(user);

    await user.type(
      screen.getByRole('textbox', { name: 'Ask about admin configuration...' }),
      'Continue reviewing deployment config.'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    expect(
      await screen.findByText(
        'Token limit exceeded for this session. Start a new assistant conversation to continue.'
      )
    ).toBeInTheDocument();
    expect(screen.queryByText(/insufficient_quota/)).not.toBeInTheDocument();
    expect(sendLlmChatWithUnifiedTools).not.toHaveBeenCalled();
  });

  it('starts a fresh assistant conversation after a context limit failure', async () => {
    const user = userEvent.setup();
    const instrumentationEvents: AdminResilienceInstrumentationEvent[] = [];
    registerAdminResilienceInstrumentationListener((event) => {
      instrumentationEvents.push(event);
    });
    const rawProviderDetail =
      'Token limit exceeded for this session. Please start a new session.';
    let streamCalls = 0;
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementation(
      async ({ sessionId, onEvent }) => {
        streamCalls += 1;
        if (streamCalls === 1) {
          onEvent('assistant_message_started', {
            message_id: 'msg-1',
            session_id: 'session-1',
          });
          try {
            onEvent('error', {
              message_id: 'msg-1',
              session_id: 'session-1',
              detail: rawProviderDetail,
            });
          } catch (error) {
            throw new StreamTurnError((error as Error).message, true);
          }
          return;
        }

        expect(sessionId).toBeNull();
        onEvent('assistant_message_started', {
          message_id: 'msg-2',
          session_id: 'session-2',
        });
        onEvent('done', { message_id: 'msg-2', session_id: 'session-2' });
      }
    );

    render(
      <ThemeProvider>
        <AdminConfigAssistant />
      </ThemeProvider>
    );
    await enableConfigTool(user);

    await user.type(
      screen.getByRole('textbox', { name: 'Ask about admin configuration...' }),
      'Continue reviewing deployment config.'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    expect(
      await screen.findByText(
        'Token limit exceeded for this session. Start a new assistant conversation to continue.'
      )
    ).toBeInTheDocument();

    const providerFailureEvents = instrumentationEvents.filter(
      (event) => event.kind === 'provider_failure'
    );
    expect(providerFailureEvents).toHaveLength(1);
    expect(providerFailureEvents[0]).toMatchObject({
      kind: 'provider_failure',
      surface: 'admin_config_assistant',
      category: 'context_limit',
      recoveryAction: 'new_assistant_conversation',
    });
    expect(JSON.stringify(providerFailureEvents[0])).not.toContain(
      rawProviderDetail
    );

    await user.click(
      screen.getByRole('button', {
        name: 'Start new assistant conversation',
      })
    );

    expect(
      screen.queryByText(
        'Token limit exceeded for this session. Start a new assistant conversation to continue.'
      )
    ).not.toBeInTheDocument();

    await user.type(
      screen.getByRole('textbox', { name: 'Ask about admin configuration...' }),
      'Review deployment config again.'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    await waitFor(() => {
      expect(sendLlmChatStreamWithUnifiedTools).toHaveBeenCalledTimes(2);
    });
  });

  it('restores the onboarding starter when starting a fresh onboarding conversation', async () => {
    const user = userEvent.setup();
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementation(
      async ({ onEvent }) => {
        onEvent('assistant_message_started', {
          message_id: 'msg-1',
          session_id: 'session-1',
        });
        onEvent('error', {
          message_id: 'msg-1',
          session_id: 'session-1',
          detail:
            'Token limit exceeded for this session. Please start a new session.',
        });
      }
    );

    render(
      <ThemeProvider>
        <AdminConfigAssistant purpose="onboarding" />
      </ThemeProvider>
    );

    expect(
      screen.getByText(/Welcome — let's set up your space/)
    ).toBeInTheDocument();

    await user.type(
      screen.getByRole('textbox', { name: 'Ask about admin configuration...' }),
      'Continue setup.'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    await user.click(
      await screen.findByRole('button', {
        name: 'Start new assistant conversation',
      })
    );

    expect(
      screen.getByText(/Welcome — let's set up your space/)
    ).toBeInTheDocument();
  });

  it('defaults to only the dedicated Config Tool without web-search', async () => {
    const user = userEvent.setup();
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementation(
      async ({ onEvent }) => {
        onEvent('assistant_message_started', {
          message_id: 'msg-1',
          session_id: 'session-1',
        });
        onEvent('answer_delta', {
          message_id: 'msg-1',
          delta: 'This is the SMTP configuration.',
        });
        onEvent('done', { message_id: 'msg-1', session_id: 'session-1' });
      }
    );

    render(
      <ThemeProvider>
        <AdminConfigAssistant />
      </ThemeProvider>
    );

    await user.type(
      screen.getByRole('textbox', { name: 'Ask about admin configuration...' }),
      'What is the SMTP configuration?'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    await waitFor(() => {
      expect(sendLlmChatStreamWithUnifiedTools).toHaveBeenCalledOnce();
    });

    const callArgs = vi.mocked(sendLlmChatStreamWithUnifiedTools).mock
      .calls[0][0];
    expect(callArgs.tools).toEqual(['admin-config']);
  });

  it('forwards apply language to Sage and refreshes direct-write areas', async () => {
    const user = userEvent.setup();
    const refreshEvents: CustomEvent[] = [];
    const recordRefresh = (event: Event) =>
      refreshEvents.push(event as CustomEvent);
    window.addEventListener(ADMIN_CONFIG_CHANGED_EVENT, recordRefresh);
    vi.mocked(sendLlmChatStreamWithUnifiedTools).mockImplementationOnce(
      async ({ onEvent }) => {
        onEvent('assistant_message_started', {
          message_id: 'msg-1',
          session_id: 'session-1',
        });
        onEvent('answer_delta', { message_id: 'msg-1', delta: 'Updated.' });
        onEvent('done', {
          message_id: 'msg-1',
          session_id: 'session-1',
          admin_config_affected_areas: [
            'agent_settings',
            'onboarding_questions',
          ],
        });
      }
    );

    render(
      <ThemeProvider>
        <AdminConfigAssistant />
      </ThemeProvider>
    );
    await user.type(
      screen.getByRole('textbox', { name: 'Ask about admin configuration...' }),
      'apply them'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    await waitFor(() =>
      expect(sendLlmChatStreamWithUnifiedTools).toHaveBeenCalledOnce()
    );
    expect(
      vi.mocked(sendLlmChatStreamWithUnifiedTools).mock.calls[0][0]
    ).toEqual(
      expect.objectContaining({
        content: 'apply them',
        tools: expect.arrayContaining(['admin-config']),
      })
    );
    expect(refreshEvents[refreshEvents.length - 1]?.detail).toEqual({
      areas: ['agent_settings', 'onboarding_questions'],
    });
    expect(mockAdminFetch.mock.calls).not.toContainEqual([
      expect.any(String),
      expect.objectContaining({
        method: expect.stringMatching(/PUT|POST|DELETE/),
      }),
    ]);
    window.removeEventListener(ADMIN_CONFIG_CHANGED_EVENT, recordRefresh);
  });
});
