import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import { InstanceConfigProvider } from '../../../context/InstanceConfigContext';
import { AdminTestAndFeedback } from '../../../pages/AdminTestAndFeedback';
import { ThemeProvider } from '../../../theme';
import { TestAsUserView } from './TestAsUserView';
import {
  sendLlmChatStreamWithUnifiedTools,
  sendLlmChatWithUnifiedTools,
} from '../../../utils/llmChat';
import {
  createSessionLog,
  deleteSessionLog,
  getImpersonationStatus,
  getSessionLog,
  listSessionLogs,
  listUserTypes,
  provisionTestUser,
  recordSessionLogPlaintextExport,
  requestImpersonationToken,
  saveTranscript,
  setMessageFeedback,
} from '../../../utils/sessionLogsApi';

vi.mock('../../../utils/llmChat', () => ({
  sendLlmChatStreamWithUnifiedTools: vi.fn(),
  sendLlmChatWithUnifiedTools: vi.fn(),
}));

vi.mock('../../../utils/sessionLogsApi', () => ({
  createSessionLog: vi.fn(),
  deleteSessionLog: vi.fn(),
  getImpersonationStatus: vi.fn(),
  getSessionLog: vi.fn(),
  listSessionLogs: vi.fn(),
  listUserTypes: vi.fn(),
  provisionTestUser: vi.fn(),
  recordSessionLogPlaintextExport: vi.fn(),
  requestImpersonationToken: vi.fn(),
  saveTranscript: vi.fn(),
  setMessageFeedback: vi.fn(),
}));

const mockSendLlmChatStreamWithUnifiedTools = vi.mocked(
  sendLlmChatStreamWithUnifiedTools
);
const mockSendLlmChatWithUnifiedTools = vi.mocked(sendLlmChatWithUnifiedTools);
const mockCreateSessionLog = vi.mocked(createSessionLog);
const mockDeleteSessionLog = vi.mocked(deleteSessionLog);
const mockGetImpersonationStatus = vi.mocked(getImpersonationStatus);
const mockGetSessionLog = vi.mocked(getSessionLog);
const mockListSessionLogs = vi.mocked(listSessionLogs);
const mockListUserTypes = vi.mocked(listUserTypes);
const mockProvisionTestUser = vi.mocked(provisionTestUser);
const mockRecordSessionLogPlaintextExport = vi.mocked(
  recordSessionLogPlaintextExport
);
const mockRequestImpersonationToken = vi.mocked(requestImpersonationToken);
const mockSaveTranscript = vi.mocked(saveTranscript);
const mockSetMessageFeedback = vi.mocked(setMessageFeedback);

type StreamChatOptions = Parameters<
  typeof sendLlmChatStreamWithUnifiedTools
>[0];

function emitStreamAnswer(
  options: StreamChatOptions,
  {
    message = 'Hello from Sage',
    sessionId = 'sage-1',
    trace,
    toolsUsed = [],
  }: {
    message?: string;
    sessionId?: string;
    trace?: unknown;
    toolsUsed?: unknown[];
  } = {}
) {
  options.onEvent('assistant_message_started', {
    message_id: 'msg-1',
    session_id: sessionId,
  });
  if (message) {
    options.onEvent('answer_delta', { delta: message, session_id: sessionId });
  }
  if (trace !== undefined) {
    options.onEvent('trace_final', { trace, session_id: sessionId });
  }
  options.onEvent('done', {
    session_id: sessionId,
    tools_used: toolsUsed,
  });
}

describe('TestAsUserView', () => {
  beforeEach(() => {
    mockSendLlmChatStreamWithUnifiedTools.mockReset();
    mockSendLlmChatWithUnifiedTools.mockReset();
    mockCreateSessionLog.mockReset();
    mockDeleteSessionLog.mockReset();
    mockGetImpersonationStatus.mockReset();
    mockGetSessionLog.mockReset();
    mockListSessionLogs.mockReset();
    mockListUserTypes.mockReset();
    mockProvisionTestUser.mockReset();
    mockRecordSessionLogPlaintextExport.mockReset();
    mockRequestImpersonationToken.mockReset();
    mockSaveTranscript.mockReset();
    mockSetMessageFeedback.mockReset();

    mockListSessionLogs.mockResolvedValue([]);
    mockListUserTypes.mockResolvedValue([
      { id: 1, name: 'Student', description: null },
    ]);
    mockProvisionTestUser.mockResolvedValue({
      user_id: 42,
      user_type_id: 1,
      created: true,
    });
    mockGetImpersonationStatus.mockResolvedValue(true);
    mockRequestImpersonationToken.mockResolvedValue({
      token: 'synthetic-user-token',
    });
    mockCreateSessionLog.mockResolvedValue({
      log_id: 'log-1',
      source: 'admin_test',
      title: 'Student trial',
      subject_user_id: 42,
      user_type_id: 1,
      sage_session_id: 'sage-1',
      turn_count: 0,
      status: 'active',
      created_by: 'admin',
      created_at: null,
      updated_at: null,
      completed_at: null,
      has_transcript: false,
    });
    mockSaveTranscript.mockResolvedValue({
      log_id: 'log-1',
      source: 'admin_test',
      title: 'Student trial',
      subject_user_id: 42,
      user_type_id: 1,
      sage_session_id: 'sage-1',
      turn_count: 2,
      status: 'completed',
      created_by: 'admin',
      created_at: null,
      updated_at: null,
      completed_at: null,
      has_transcript: true,
    });
    mockSendLlmChatStreamWithUnifiedTools.mockImplementation(
      async (options) => {
        emitStreamAnswer(options);
      }
    );
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
        if (String(input).includes('/session-defaults')) {
          return Response.json({
            web_search_enabled: true,
            default_document_ids: ['doc-1', 'doc-2'],
            default_tool_ids: ['curated-resources', 'web-search'],
            knowledge_source_scope: 'selected',
          });
        }
        return new Response(null, { status: 404 });
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
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  async function startStudentSession(onSaved?: () => void) {
    const user = userEvent.setup();
    render(
      <ThemeProvider>
        <InstanceConfigProvider>
          <TestAsUserView onSaved={onSaved} />
        </InstanceConfigProvider>
      </ThemeProvider>
    );

    await screen.findByRole('option', { name: 'Student' });
    await user.selectOptions(screen.getByLabelText('User type'), '1');
    await user.click(screen.getByRole('button', { name: 'Start session' }));

    return user;
  }

  it('keeps an active test chat scoped to the synthetic User identity', async () => {
    await startStudentSession();

    expect(await screen.findByText('Testing as Student')).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Admin' })
    ).not.toBeInTheDocument();
  });

  it('keeps the active test chat in a viewport-bounded scrolling workspace', async () => {
    await startStudentSession();

    const workspace = await screen.findByRole('region', {
      name: 'Test User conversation workspace',
    });
    const scrollViewport = screen.getByRole('group', {
      name: 'Conversation messages',
    });
    const composer = screen.getByRole('textbox', {
      name: 'Message the assistant as this user…',
    });
    const reset = screen.getByRole('button', { name: 'Reset' });
    const exit = screen.getByRole('button', { name: 'Exit' });
    const save = screen.getByRole('button', { name: 'End & save trial' });
    const workspaceHeightClass = Array.from(workspace.classList).find(
      (className) => className.startsWith('h-[clamp(')
    );

    expect(workspaceHeightClass).toMatch(/32rem.*100dvh.*56rem/);
    expect(workspace).toHaveClass('overflow-hidden');
    expect(scrollViewport).toHaveClass('overflow-y-auto');
    expect(workspace).toContainElement(scrollViewport);
    expect(workspace).toContainElement(composer);
    expect(workspace).toContainElement(reset);
    expect(workspace).toContainElement(exit);
    expect(workspace).toContainElement(save);
    expect(scrollViewport).not.toContainElement(composer);
    expect(scrollViewport).not.toContainElement(reset);
    expect(scrollViewport).not.toContainElement(exit);
    expect(scrollViewport).not.toContainElement(save);
  });

  it('renders markdown plus live Activity and Trace after early text while the shared surface remains running', async () => {
    let finishStream!: () => void;
    const streamGate = new Promise<void>((resolve) => {
      finishStream = resolve;
    });
    mockSendLlmChatStreamWithUnifiedTools.mockImplementationOnce(
      async (options) => {
        options.onEvent('assistant_message_started', {
          message_id: 'msg-1',
          session_id: 'sage-1',
        });
        options.onEvent('answer_delta', {
          delta: 'Early **answer**',
          session_id: 'sage-1',
        });
        options.onEvent('activity_step', {
          activity_step: {
            id: 'resource-search',
            kind: 'tool',
            title: 'Searching resources',
            status: 'running',
          },
        });
        options.onEvent('trace_delta', {
          trace_delta: {
            id: 'resource-result',
            kind: 'tool_result',
            title: 'Resources ready',
            status: 'succeeded',
          },
        });
        options.onEvent('trace_delta', {
          trace_delta: {
            id: 'model-request',
            kind: 'timing',
            title: 'Model request',
            content: 'Model request: 820 ms.',
            status: 'succeeded',
          },
        });
        await streamGate;
        options.onEvent('done', { session_id: 'sage-1', tools_used: [] });
      }
    );
    const user = await startStudentSession();

    await user.type(
      screen.getByRole('textbox', {
        name: 'Message the assistant as this user…',
      }),
      'Find resources'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    const answer = await screen.findByText('answer');
    expect(answer.tagName).toBe('STRONG');
    expect(await screen.findByText('Searching resources')).toBeInTheDocument();
    expect(await screen.findByText('Resources ready')).toBeInTheDocument();
    expect(
      screen.queryByText('Model request: 820 ms.')
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole('textbox', {
        name: 'Message the assistant as this user…',
      })
    ).toBeDisabled();

    await act(async () => {
      finishStream();
      await streamGate;
    });

    await waitFor(() =>
      expect(
        screen.getByRole('textbox', {
          name: 'Message the assistant as this user…',
        })
      ).toBeEnabled()
    );
  });

  it('sends chat turns with the synthetic User bearer token', async () => {
    const user = await startStudentSession();

    await user.type(
      screen.getByPlaceholderText('Message the assistant as this user…'),
      'Can you help me?'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    await waitFor(() => {
      expect(mockSendLlmChatStreamWithUnifiedTools).toHaveBeenCalledWith(
        expect.objectContaining({
          content: 'Can you help me?',
          authToken: 'synthetic-user-token',
        })
      );
    });
  });

  it('uses the shared pre-output fallback with the synthetic User bearer', async () => {
    mockSendLlmChatStreamWithUnifiedTools.mockRejectedValueOnce(
      new Error('Stream unavailable')
    );
    mockSendLlmChatWithUnifiedTools.mockResolvedValueOnce(
      Response.json({
        message_id: 'msg-fallback',
        message: 'Fallback answer',
        session_id: 'sage-fallback',
        trace: null,
        tools_used: [],
      })
    );
    const user = await startStudentSession();

    await user.type(
      screen.getByPlaceholderText('Message the assistant as this user…'),
      'Use fallback'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    expect(await screen.findByText('Fallback answer')).toBeInTheDocument();
    expect(mockSendLlmChatWithUnifiedTools).toHaveBeenCalledWith(
      expect.objectContaining({
        content: 'Use fallback',
        authToken: 'synthetic-user-token',
      })
    );
    expect(screen.queryByText('Stream unavailable')).not.toBeInTheDocument();
  });

  it('shows live stream status before the streamed answer finishes', async () => {
    let finishStream!: () => void;
    const streamGate = new Promise<void>((resolve) => {
      finishStream = resolve;
    });
    mockSendLlmChatStreamWithUnifiedTools.mockImplementationOnce(
      async (options) => {
        options.onEvent('assistant_message_started', {
          message_id: 'msg-1',
          session_id: 'sage-1',
        });
        options.onEvent('trace_status', {
          status: 'Running enabled tools...',
          session_id: 'sage-1',
        });
        await streamGate;
        options.onEvent('answer_delta', {
          delta: 'I found resources.',
          session_id: 'sage-1',
        });
        options.onEvent('done', { session_id: 'sage-1', tools_used: [] });
      }
    );
    const user = await startStudentSession();

    await user.type(
      screen.getByPlaceholderText('Message the assistant as this user…'),
      'Find resources'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    expect(
      await screen.findByText('Running enabled tools...')
    ).toBeInTheDocument();

    await act(async () => {
      finishStream();
      await streamGate;
    });

    expect(await screen.findByText('I found resources.')).toBeInTheDocument();
    expect(
      screen.queryByText('Running enabled tools...')
    ).not.toBeInTheDocument();
  });

  it('sends real user default Tool Sets and document constraints while impersonating the synthetic User', async () => {
    const user = await startStudentSession();

    await user.type(
      screen.getByPlaceholderText('Message the assistant as this user…'),
      'Do you have any resources you can read through?'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    await waitFor(() => {
      expect(mockSendLlmChatStreamWithUnifiedTools).toHaveBeenCalledWith(
        expect.objectContaining({
          content: 'Do you have any resources you can read through?',
          tools: expect.arrayContaining([
            'curated-resources',
            'knowledge-search',
            'web-search',
          ]),
          jobIds: ['doc-1', 'doc-2'],
          authToken: 'synthetic-user-token',
        })
      );
    });
    const lastCall =
      mockSendLlmChatStreamWithUnifiedTools.mock.calls[
        mockSendLlmChatStreamWithUnifiedTools.mock.calls.length - 1
      ];
    const request = lastCall?.[0];
    expect([...(request?.tools ?? [])].sort()).toEqual(
      ['curated-resources', 'knowledge-search', 'web-search'].sort()
    );
    expect(request?.tools).not.toContain('admin-config');
    expect(request?.tools).not.toContain('db-query');
    expect(fetch).toHaveBeenCalledWith('/api/session-defaults?user_type_id=1', {
      credentials: 'include',
    });
  });

  it('sends all-knowledge defaults without selected document constraints while impersonating the synthetic User', async () => {
    vi.mocked(fetch).mockImplementation(async (input) =>
      String(input).includes('/session-defaults')
        ? Response.json({
            web_search_enabled: false,
            default_document_ids: [],
            default_tool_ids: ['curated-resources', 'knowledge-search'],
            knowledge_source_scope: 'all',
          })
        : new Response(null, { status: 404 })
    );
    const user = await startStudentSession();

    await user.type(
      screen.getByPlaceholderText('Message the assistant as this user…'),
      'Can you search all available knowledge?'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    await waitFor(() => {
      expect(mockSendLlmChatStreamWithUnifiedTools).toHaveBeenCalledWith(
        expect.objectContaining({
          content: 'Can you search all available knowledge?',
          tools: expect.arrayContaining([
            'curated-resources',
            'knowledge-search',
          ]),
          jobIds: [],
          authToken: 'synthetic-user-token',
        })
      );
    });
    const lastCall =
      mockSendLlmChatStreamWithUnifiedTools.mock.calls[
        mockSendLlmChatStreamWithUnifiedTools.mock.calls.length - 1
      ];
    const request = lastCall?.[0];
    expect([...(request?.tools ?? [])].sort()).toEqual(
      ['curated-resources', 'knowledge-search'].sort()
    );
  });

  it('uses a conservative Tool Set fallback when user defaults cannot be loaded', async () => {
    vi.mocked(fetch).mockImplementation(async (input) =>
      String(input).includes('/session-defaults')
        ? new Response(null, { status: 500 })
        : new Response(null, { status: 404 })
    );
    const user = await startStudentSession();

    await user.type(
      screen.getByPlaceholderText('Message the assistant as this user…'),
      'Can you look up resources?'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    await waitFor(() => {
      expect(mockSendLlmChatStreamWithUnifiedTools).toHaveBeenCalledWith(
        expect.objectContaining({
          content: 'Can you look up resources?',
          tools: [],
          jobIds: [],
          authToken: 'synthetic-user-token',
        })
      );
    });
  });

  it('preserves useful partial output but keeps the unfinished turn out of the transcript', async () => {
    const completedTrace = {
      visibility: 'detailed' as const,
      tools: [
        {
          id: 'curated-resources',
          name: 'Curated Resources',
          status: 'succeeded',
        },
      ],
      retrieval: [],
    };
    const completedTools = [
      {
        tool_id: 'curated-resources',
        tool_name: 'Curated Resources',
        warnings: [],
        guarded: false,
      },
    ];
    mockSendLlmChatStreamWithUnifiedTools
      .mockImplementationOnce(async (options) => {
        emitStreamAnswer(options, {
          message: 'Completed answer',
          trace: completedTrace,
          toolsUsed: completedTools,
        });
      })
      .mockImplementationOnce(async (options) => {
        options.onEvent('assistant_message_started', {
          message_id: 'msg-2',
          session_id: 'sage-1',
        });
        options.onEvent('answer_delta', {
          delta: 'Partial answer',
          session_id: 'sage-1',
        });
        throw new Error('Sage stream failed');
      });
    const user = await startStudentSession();

    await user.type(
      screen.getByPlaceholderText('Message the assistant as this user…'),
      'This turn will complete'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));
    expect(await screen.findByText('Completed answer')).toBeInTheDocument();

    await user.type(
      screen.getByPlaceholderText('Message the assistant as this user…'),
      'This stream will fail'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    expect(await screen.findByText('Partial answer')).toBeInTheDocument();
    expect(await screen.findByText('Sage stream failed')).toBeInTheDocument();
    expect(
      screen.getByPlaceholderText('Message the assistant as this user…')
    ).toBeEnabled();
    expect(mockSendLlmChatWithUnifiedTools).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: 'End & save trial' }));
    await waitFor(() => expect(mockSaveTranscript).toHaveBeenCalled());
    expect(mockSaveTranscript.mock.calls[0]?.[1]).toEqual([
      expect.objectContaining({
        role: 'user',
        content: 'This turn will complete',
      }),
      expect.objectContaining({
        role: 'assistant',
        content: 'Completed answer',
        message_id: 'msg-1',
        trace: completedTrace,
        tools_used: completedTools,
      }),
    ]);
  });

  it('saves a later completed exchange after an earlier request fails before output', async () => {
    mockSendLlmChatStreamWithUnifiedTools
      .mockRejectedValueOnce(new Error('Stream unavailable'))
      .mockImplementationOnce(async (options) => {
        emitStreamAnswer(options, {
          message: 'Later completed answer',
          sessionId: 'sage-later',
        });
      });
    mockSendLlmChatWithUnifiedTools.mockResolvedValueOnce(
      Response.json({ detail: 'Fallback unavailable' }, { status: 503 })
    );
    const user = await startStudentSession();

    await user.type(
      screen.getByPlaceholderText('Message the assistant as this user…'),
      'Failed question'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));
    expect(await screen.findByText('Fallback unavailable')).toBeInTheDocument();

    await user.type(
      screen.getByPlaceholderText('Message the assistant as this user…'),
      'Successful question'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));
    expect(
      await screen.findByText('Later completed answer')
    ).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'End & save trial' }));
    await waitFor(() => expect(mockSaveTranscript).toHaveBeenCalled());
    expect(mockSaveTranscript.mock.calls[0]?.[1]).toEqual([
      expect.objectContaining({
        role: 'user',
        content: 'Successful question',
      }),
      expect.objectContaining({
        role: 'assistant',
        content: 'Later completed answer',
      }),
    ]);
  });

  it('pairs a fallback answer with its question after activity-only stream output', async () => {
    mockSendLlmChatStreamWithUnifiedTools.mockImplementationOnce(
      async (options) => {
        options.onEvent('assistant_message_started', {
          message_id: 'stream-assistant',
          session_id: 'sage-fallback',
        });
        options.onEvent('activity_step', {
          activity_step: {
            id: 'searching',
            kind: 'tool',
            title: 'Searching resources',
            status: 'running',
          },
        });
        throw new Error('Stream unavailable');
      }
    );
    mockSendLlmChatWithUnifiedTools.mockResolvedValueOnce(
      Response.json({
        message_id: 'fallback-assistant',
        message: 'Fallback completed answer',
        session_id: 'sage-fallback',
        tools_used: [],
      })
    );
    const user = await startStudentSession();

    await user.type(
      screen.getByPlaceholderText('Message the assistant as this user…'),
      'Question needing fallback'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));
    expect(
      await screen.findByText('Fallback completed answer')
    ).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'End & save trial' }));
    await waitFor(() => expect(mockSaveTranscript).toHaveBeenCalled());
    expect(mockSaveTranscript.mock.calls[0]?.[1]).toEqual([
      expect.objectContaining({
        role: 'user',
        content: 'Question needing fallback',
      }),
      expect.objectContaining({
        role: 'assistant',
        content: 'Fallback completed answer',
        message_id: 'fallback-assistant',
      }),
    ]);
  });

  it('does not start a test chat when synthetic User auth is unavailable', async () => {
    const user = userEvent.setup();
    mockGetImpersonationStatus.mockResolvedValue(false);

    render(<TestAsUserView />);

    await screen.findByRole('option', { name: 'Student' });
    await user.selectOptions(screen.getByLabelText('User type'), '1');
    await user.click(screen.getByRole('button', { name: 'Start session' }));

    expect(
      await screen.findByText('Test-user impersonation is not available yet')
    ).toBeInTheDocument();
    expect(mockProvisionTestUser).not.toHaveBeenCalled();
    expect(screen.queryByText('Testing as Student')).not.toBeInTheDocument();
  });

  it('resets the active test conversation without changing the synthetic User identity', async () => {
    const confirm = vi
      .spyOn(window, 'confirm')
      .mockReturnValueOnce(false)
      .mockReturnValueOnce(true);
    mockSendLlmChatStreamWithUnifiedTools
      .mockImplementationOnce(async (options) => {
        emitStreamAnswer(options, {
          message: 'First answer',
          sessionId: 'sage-1',
        });
      })
      .mockImplementationOnce(async (options) => {
        emitStreamAnswer(options, {
          message: 'Second answer',
          sessionId: 'sage-2',
        });
      });
    const user = await startStudentSession();

    await user.type(
      screen.getByPlaceholderText('Message the assistant as this user…'),
      'First message'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));
    expect(await screen.findByText('First answer')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Reset' }));
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(screen.getByText('First answer')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Reset' }));
    expect(confirm).toHaveBeenCalledTimes(2);

    expect(screen.queryByText('First answer')).not.toBeInTheDocument();
    expect(
      screen.getByRole('heading', { name: 'What would you like to know?' })
    ).toBeInTheDocument();

    await user.type(
      screen.getByPlaceholderText('Message the assistant as this user…'),
      'Second message'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    await waitFor(() => {
      expect(mockSendLlmChatStreamWithUnifiedTools).toHaveBeenCalledTimes(2);
    });
    expect(mockSendLlmChatStreamWithUnifiedTools.mock.calls[1][0]).toEqual(
      expect.objectContaining({
        content: 'Second message',
        sessionId: null,
        authToken: 'synthetic-user-token',
      })
    );
  });

  it('exits the active test session back to the persona picker', async () => {
    const user = await startStudentSession();

    expect(await screen.findByText('Testing as Student')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Exit' }));

    expect(screen.getByText('Pick a persona to test')).toBeInTheDocument();
    expect(screen.queryByText('Testing as Student')).not.toBeInTheDocument();
    expect(
      screen.queryByText('Send a message as this user to begin the trial.')
    ).not.toBeInTheDocument();
  });

  it('confirms before discarding completed unsaved turns', async () => {
    const confirm = vi
      .spyOn(window, 'confirm')
      .mockReturnValueOnce(false)
      .mockReturnValueOnce(true);
    const user = await startStudentSession();

    await user.type(
      screen.getByPlaceholderText('Message the assistant as this user…'),
      'Keep this completed turn'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));
    expect(await screen.findByText('Hello from Sage')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Exit' }));
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(screen.getByText('Testing as Student')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Exit' }));
    expect(confirm).toHaveBeenCalledTimes(2);
    expect(screen.getByText('Pick a persona to test')).toBeInTheDocument();
  });

  it('does not save a transcript while a chat response is still pending', async () => {
    let resolveChat!: () => void;
    mockSendLlmChatStreamWithUnifiedTools.mockImplementationOnce(
      (options) =>
        new Promise<void>((resolve) => {
          resolveChat = () => {
            emitStreamAnswer(options, { message: 'Done', sessionId: 'sage-1' });
            resolve();
          };
        })
    );
    const user = await startStudentSession();

    await user.type(
      screen.getByPlaceholderText('Message the assistant as this user…'),
      'Hold this save until Sage replies'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    const saveButton = screen.getByRole('button', { name: 'End & save trial' });
    await waitFor(() => {
      expect(saveButton).toBeDisabled();
    });
    await user.click(saveButton);
    expect(mockCreateSessionLog).not.toHaveBeenCalled();
    expect(mockSaveTranscript).not.toHaveBeenCalled();

    await act(async () => {
      resolveChat();
    });
    expect(await screen.findByText('Done')).toBeInTheDocument();
    expect(saveButton).not.toBeDisabled();
  });

  it('reuses the pending encrypted log when transcript saving is retried', async () => {
    mockSaveTranscript.mockRejectedValueOnce(
      new Error('Encrypted transcript save failed')
    );
    const user = await startStudentSession();

    await user.type(
      screen.getByPlaceholderText('Message the assistant as this user…'),
      'Save this exchange once'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));
    expect(await screen.findByText('Hello from Sage')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'End & save trial' }));
    expect(
      await screen.findByText('Encrypted transcript save failed')
    ).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'End & save trial' }));
    await waitFor(() => expect(mockSaveTranscript).toHaveBeenCalledTimes(2));

    expect(mockCreateSessionLog).toHaveBeenCalledTimes(1);
    expect(mockSaveTranscript.mock.calls[0]?.[0]).toBe('log-1');
    expect(mockSaveTranscript.mock.calls[1]?.[0]).toBe('log-1');
  });

  it('preserves Sage trace and tool metadata when saving the test transcript', async () => {
    const onSaved = vi.fn();
    const toolsUsed = [
      {
        tool_id: 'curated-resources',
        tool_name: 'Curated Resources',
        query: 'Nicaragua political detention legal aid',
        output_summary: 'Found 2 vetted resources.',
      },
    ];
    const trace = {
      visibility: 'detailed',
      reasoning: {
        summary: 'Sage used enabled tools before answering.',
      },
      tools: [
        {
          id: 'curated-resources',
          name: 'Curated Resources',
          status: 'succeeded',
          output_summary: 'Found 2 vetted resources.',
        },
      ],
      retrieval: [],
    };
    mockSendLlmChatStreamWithUnifiedTools.mockImplementationOnce(
      async (options) => {
        emitStreamAnswer(options, {
          message: 'I found vetted resources.',
          sessionId: 'sage-1',
          trace,
          toolsUsed,
        });
      }
    );
    const user = await startStudentSession(onSaved);

    await user.type(
      screen.getByPlaceholderText('Message the assistant as this user…'),
      'Find resources for me'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));
    expect(
      await screen.findByText('I found vetted resources.')
    ).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'End & save trial' }));

    await waitFor(() => {
      expect(mockSaveTranscript).toHaveBeenCalledWith(
        'log-1',
        expect.arrayContaining([
          expect.objectContaining({
            role: 'assistant',
            content: 'I found vetted resources.',
            message_id: 'msg-1',
            tools_used: [
              expect.objectContaining({
                tool_id: 'curated-resources',
                tool_name: 'Curated Resources',
              }),
            ],
            trace: expect.objectContaining({
              tools: [
                expect.objectContaining({
                  id: 'curated-resources',
                  name: 'Curated Resources',
                }),
              ],
            }),
          }),
        ]),
        expect.any(String)
      );
    });
    expect(onSaved).toHaveBeenCalledTimes(1);
  });

  it('navigates the parent Test & Feedback page to Feedback after saving', async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <ThemeProvider>
          <InstanceConfigProvider>
            <AdminTestAndFeedback />
          </InstanceConfigProvider>
        </ThemeProvider>
      </MemoryRouter>
    );

    await screen.findByRole('option', { name: 'Student' });
    await user.selectOptions(screen.getByLabelText('User type'), '1');
    await user.click(screen.getByRole('button', { name: 'Start session' }));
    await user.type(
      screen.getByPlaceholderText('Message the assistant as this user…'),
      'Save this trial'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));
    expect(await screen.findByText('Hello from Sage')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'End & save trial' }));

    expect(
      await screen.findByRole('tab', { name: 'Feedback', selected: true })
    ).toBeInTheDocument();
    expect(
      await screen.findByText(
        'No saved beta logs yet. Run a Test User Session or wait for user conversations.'
      )
    ).toBeInTheDocument();
  });
});
