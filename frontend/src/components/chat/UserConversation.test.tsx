import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createRef } from 'react';
import { InstanceConfigProvider } from '../../context/InstanceConfigContext';
import { ThemeProvider } from '../../theme';
import {
  sendLlmChatStreamWithUnifiedTools,
  sendLlmChatWithUnifiedTools,
} from '../../utils/llmChat';
import {
  UserConversation,
  type UserConversationHandle,
  type UserConversationTerminalTurn,
} from './UserConversation';

vi.mock('../../utils/llmChat', () => ({
  sendLlmChatStreamWithUnifiedTools: vi.fn(),
  sendLlmChatWithUnifiedTools: vi.fn(),
}));

const mockStream = vi.mocked(sendLlmChatStreamWithUnifiedTools);
const mockSend = vi.mocked(sendLlmChatWithUnifiedTools);

function renderConversation(
  onTerminalTurn = vi.fn<(turn: UserConversationTerminalTurn) => void>()
) {
  render(
    <ThemeProvider>
      <InstanceConfigProvider>
        <UserConversation
          selectedTools={['curated-resources']}
          selectedDocuments={['doc-1']}
          onTerminalTurn={onTerminalTurn}
        />
      </InstanceConfigProvider>
    </ThemeProvider>
  );
  return { onTerminalTurn };
}

describe('UserConversation', () => {
  beforeEach(() => {
    mockStream.mockReset();
    mockSend.mockReset();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false }));
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
    vi.unstubAllGlobals();
  });

  it('owns streamed Activity, Trace, answer ordering, and terminal session metadata', async () => {
    const trace = {
      visibility: 'detailed' as const,
      tools: [],
      retrieval: [],
      activity_steps: [],
    };
    mockStream.mockImplementationOnce(async ({ onEvent }) => {
      onEvent('assistant_message_started', {
        message_id: 'assistant-1',
        session_id: 'session-1',
      });
      onEvent('activity_step', {
        activity_step: {
          id: 'resource-search',
          kind: 'tool',
          title: 'Searching resources',
          status: 'running',
        },
      });
      onEvent('trace_delta', {
        trace_delta: {
          id: 'resource-result',
          kind: 'tool_result',
          title: 'Resources ready',
          status: 'succeeded',
        },
      });
      onEvent('answer_delta', { delta: 'Here is **help**.' });
      onEvent('trace_final', { trace });
      onEvent('done', {
        session_id: 'session-1',
        tools_used: [
          {
            tool_id: 'curated-resources',
            tool_name: 'Curated Resources',
            query: 'legal help',
          },
          { name: 'malformed-tool-record' },
        ],
      });
    });
    const user = userEvent.setup();
    const { onTerminalTurn } = renderConversation();

    await user.type(
      screen.getByRole('textbox', { name: 'Ask anything...' }),
      'Find help'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    const activity = await screen.findByText('Searching resources');
    const traceDelta = await screen.findByText('Resources ready');
    const answer = await screen.findByText('help');
    expect(
      activity.compareDocumentPosition(traceDelta) &
        Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy();
    expect(
      traceDelta.compareDocumentPosition(answer) &
        Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy();
    await waitFor(() => {
      expect(onTerminalTurn).toHaveBeenCalledWith({
        userTurnId: expect.stringMatching(/^user-/),
        assistantTurnId: 'assistant-1',
        sessionId: 'session-1',
        toolsUsed: [
          {
            tool_id: 'curated-resources',
            tool_name: 'Curated Resources',
            query: 'legal help',
            warnings: [],
            guarded: false,
          },
        ],
      });
    });
    expect(mockStream).toHaveBeenCalledWith(
      expect.objectContaining({
        content: 'Find help',
        tools: ['curated-resources'],
        jobIds: ['doc-1'],
        sessionId: null,
      })
    );
    expect(mockSend).not.toHaveBeenCalled();
  });

  it('uses the bounded non-streaming fallback when streaming fails before useful output', async () => {
    mockStream.mockRejectedValueOnce(new Error('Stream unavailable'));
    mockSend.mockResolvedValueOnce(
      Response.json({
        message_id: 'assistant-fallback',
        message: 'Fallback answer',
        session_id: 'session-fallback',
        trace: { visibility: 'summary', tools: [], retrieval: [] },
      })
    );
    const user = userEvent.setup();
    const { onTerminalTurn } = renderConversation();

    await user.type(
      screen.getByRole('textbox', { name: 'Ask anything...' }),
      'Please answer'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    expect(await screen.findByText('Fallback answer')).toBeInTheDocument();
    expect(mockSend).toHaveBeenCalledWith(
      expect.objectContaining({
        content: 'Please answer',
        sessionId: null,
        tools: ['curated-resources'],
        jobIds: ['doc-1'],
      })
    );
    expect(onTerminalTurn).toHaveBeenCalledWith({
      userTurnId: expect.stringMatching(/^user-/),
      assistantTurnId: 'assistant-fallback',
      sessionId: 'session-fallback',
      toolsUsed: [],
    });
    expect(screen.queryByText('Stream unavailable')).not.toBeInTheDocument();
  });

  it('preserves useful partial output and recovers the composer after a stream failure', async () => {
    mockStream.mockImplementationOnce(async ({ onEvent }) => {
      onEvent('assistant_message_started', {
        message_id: 'assistant-partial',
        session_id: 'session-partial',
      });
      onEvent('answer_delta', { delta: 'Useful partial answer' });
      throw new Error('Connection interrupted');
    });
    const user = userEvent.setup();
    renderConversation();

    await user.type(
      screen.getByRole('textbox', { name: 'Ask anything...' }),
      'Start an answer'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    expect(
      await screen.findByText('Useful partial answer')
    ).toBeInTheDocument();
    expect(
      await screen.findByText('Connection interrupted')
    ).toBeInTheDocument();
    expect(
      screen.getByRole('textbox', { name: 'Ask anything...' })
    ).toBeEnabled();
    expect(mockSend).not.toHaveBeenCalled();
  });

  it('does not expose sensitive provider detail from a streamed error event', async () => {
    mockStream.mockImplementationOnce(async ({ onEvent }) => {
      onEvent('error', {
        detail: 'Authorization: Bearer secret-provider-token',
      });
    });
    const user = userEvent.setup();
    renderConversation();

    await user.type(
      screen.getByRole('textbox', { name: 'Ask anything...' }),
      'Please answer'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    expect(
      await screen.findByText(
        'The Model Provider request failed. Try again or start a new assistant conversation.'
      )
    ).toBeInTheDocument();
    expect(
      screen.queryByText('Authorization: Bearer secret-provider-token')
    ).not.toBeInTheDocument();
    expect(mockSend).not.toHaveBeenCalled();
  });

  it('does not publish terminal evidence when a stream ends before done', async () => {
    mockStream.mockImplementationOnce(async ({ onEvent }) => {
      onEvent('assistant_message_started', {
        message_id: 'assistant-truncated',
        session_id: 'session-truncated',
      });
      onEvent('answer_delta', { delta: 'Useful but incomplete answer' });
    });
    const user = userEvent.setup();
    const { onTerminalTurn } = renderConversation();

    await user.type(
      screen.getByRole('textbox', { name: 'Ask anything...' }),
      'Start an answer'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    expect(
      await screen.findByText('Useful but incomplete answer')
    ).toBeInTheDocument();
    expect(
      await screen.findByRole('note', { name: 'Chat request error' })
    ).toBeInTheDocument();
    expect(onTerminalTurn).not.toHaveBeenCalled();
    expect(mockSend).not.toHaveBeenCalled();
  });

  it('preserves the bounded fallback response detail when both transports fail', async () => {
    mockStream.mockRejectedValueOnce(new Error('Stream unavailable'));
    mockSend.mockResolvedValueOnce(
      Response.json(
        { detail: 'Model Provider is temporarily unavailable' },
        { status: 503 }
      )
    );
    const user = userEvent.setup();
    renderConversation();

    await user.type(
      screen.getByRole('textbox', { name: 'Ask anything...' }),
      'Please answer'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    expect(
      await screen.findByText('Model Provider is temporarily unavailable')
    ).toBeInTheDocument();
    expect(
      screen.getByRole('textbox', { name: 'Ask anything...' })
    ).toBeEnabled();
  });

  it('hydrates and resets a Sage Conversation through the adapter handle', async () => {
    mockStream.mockImplementationOnce(async ({ onEvent }) => {
      onEvent('assistant_message_started', {
        message_id: 'assistant-next',
        session_id: 'session-existing',
      });
      onEvent('answer_delta', { delta: 'Continued answer' });
      onEvent('done', { session_id: 'session-existing' });
    });
    const handle = createRef<UserConversationHandle>();
    render(
      <ThemeProvider>
        <InstanceConfigProvider>
          <UserConversation
            ref={handle}
            selectedTools={['curated-resources']}
            selectedDocuments={[]}
          />
        </InstanceConfigProvider>
      </ThemeProvider>
    );

    act(() => {
      handle.current?.hydrate('session-existing', [
        {
          id: 'user-existing',
          role: 'user',
          content: 'Earlier question',
          activitySteps: [],
          traceDeltas: [],
          trace: null,
          traceStatus: null,
        },
        {
          id: 'assistant-existing',
          role: 'assistant',
          content: 'Earlier answer',
          activitySteps: [],
          traceDeltas: [],
          trace: null,
          traceStatus: null,
        },
      ]);
    });
    expect(await screen.findByText('Earlier answer')).toBeInTheDocument();

    const user = userEvent.setup();
    await user.type(
      screen.getByRole('textbox', { name: 'Ask anything...' }),
      'Continue'
    );
    await user.click(screen.getByRole('button', { name: 'Send message' }));

    expect(await screen.findByText('Continued answer')).toBeInTheDocument();
    expect(mockStream).toHaveBeenCalledWith(
      expect.objectContaining({ sessionId: 'session-existing' })
    );
    // Sage owns the Conversation history; the browser sends only the new message.
    const [latestRequest] =
      mockStream.mock.calls[mockStream.mock.calls.length - 1];
    expect(latestRequest).not.toHaveProperty('conversationHistory');

    act(() => handle.current?.reset());
    expect(screen.queryByText('Earlier answer')).not.toBeInTheDocument();
    expect(screen.queryByText('Continued answer')).not.toBeInTheDocument();
    expect(
      screen.getByRole('heading', { name: 'What would you like to know?' })
    ).toBeInTheDocument();
  });
});
