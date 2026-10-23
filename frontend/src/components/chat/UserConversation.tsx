import {
  forwardRef,
  type ReactNode,
  useCallback,
  useEffect,
  useImperativeHandle,
  useReducer,
} from 'react';
import { useTranslation } from 'react-i18next';
import { AlertCircle, X } from 'lucide-react';
import {
  sendLlmChatStreamWithUnifiedTools,
  sendLlmChatWithUnifiedTools,
} from '../../utils/llmChat';
import {
  classifyProviderError,
  formatClassifiedProviderError,
} from '../../utils/providerErrors';
import { Callout, IconButton } from '../ui';
import type { ConversationTrace } from './ChatMessage';
import { ConversationSurface } from './ConversationSurface';
import { buildConversationSurfaceTurns } from './ConversationSurfaceModel';
import {
  createConversationUiState,
  reduceConversationUiState,
  type ConversationUiState,
  type ConversationUiTurn,
} from './ConversationUiState';
import { adaptSageStreamEvent } from './SageStreamEventAdapter';

export interface UserConversationTerminalTurn {
  userTurnId: string;
  assistantTurnId: string;
  sessionId: string | null;
  toolsUsed: UserConversationToolUse[];
}

export interface UserConversationToolUse {
  tool_id: string;
  tool_name: string;
  query?: string | null;
  output_summary?: string | null;
  warnings: string[];
  guarded: boolean;
}

export interface UserConversationHandle {
  reset: () => void;
  hydrate: (sessionId: string, turns: ConversationUiTurn[]) => void;
  fail: (message: string) => void;
  dismissError: () => void;
  getSnapshot: () => ConversationUiState;
}

export interface UserConversationProps {
  selectedTools: string[];
  selectedDocuments: string[];
  authToken?: string | null;
  placeholder?: string;
  toolbar?: ReactNode;
  notices?: ReactNode;
  onTerminalTurn?: (turn: UserConversationTerminalTurn) => void;
  onSnapshot?: (state: ConversationUiState) => void;
  onAuthFailure?: (status: 401 | 403) => void;
}

let localTurnSequence = 0;

function generateTurnId(role: 'user' | 'assistant'): string {
  if (typeof globalThis.crypto?.randomUUID === 'function') {
    return `${role}-${globalThis.crypto.randomUUID()}`;
  }
  localTurnSequence += 1;
  return `${role}-${Date.now()}-${localTurnSequence}`;
}

function payloadRecord(payload: unknown): Record<string, unknown> {
  return payload && typeof payload === 'object'
    ? (payload as Record<string, unknown>)
    : {};
}

function readToolUses(value: unknown): UserConversationToolUse[] {
  if (!Array.isArray(value)) return [];

  return value.flatMap((candidate) => {
    const tool = payloadRecord(candidate);
    if (
      typeof tool.tool_id !== 'string' ||
      !tool.tool_id ||
      typeof tool.tool_name !== 'string' ||
      !tool.tool_name
    ) {
      return [];
    }

    return [
      {
        tool_id: tool.tool_id,
        tool_name: tool.tool_name,
        query:
          typeof tool.query === 'string' || tool.query === null
            ? tool.query
            : undefined,
        output_summary:
          typeof tool.output_summary === 'string' ||
          tool.output_summary === null
            ? tool.output_summary
            : undefined,
        warnings: Array.isArray(tool.warnings)
          ? tool.warnings.filter(
              (warning): warning is string => typeof warning === 'string'
            )
          : [],
        guarded: typeof tool.guarded === 'boolean' ? tool.guarded : false,
      },
    ];
  });
}

function providerStreamErrorMessage(
  detail: unknown,
  fallbackMessage: string
): string {
  const classified = classifyProviderError(detail);
  if (classified.category === 'unknown' && typeof detail !== 'string') {
    return fallbackMessage;
  }
  return formatClassifiedProviderError(classified);
}

async function responseErrorMessage(response: Response): Promise<string> {
  const fallback = `HTTP ${response.status}`;
  try {
    const contentType = response.headers.get('content-type') ?? '';
    const detail = contentType.includes('application/json')
      ? await response.json()
      : (await response.text()).trim() || fallback;
    return formatClassifiedProviderError(classifyProviderError(detail));
  } catch {
    return fallback;
  }
}

export const UserConversation = forwardRef<
  UserConversationHandle,
  UserConversationProps
>(function UserConversation(
  {
    selectedTools,
    selectedDocuments,
    authToken,
    placeholder,
    toolbar,
    notices,
    onTerminalTurn,
    onSnapshot,
    onAuthFailure,
  },
  ref
) {
  const { t } = useTranslation();
  const [state, dispatch] = useReducer(
    reduceConversationUiState,
    undefined,
    () =>
      createConversationUiState({
        selectedTools,
        selectedDocuments,
      })
  );

  useEffect(() => {
    dispatch({ type: 'selectedToolsChanged', selectedTools });
  }, [selectedTools]);

  useEffect(() => {
    dispatch({ type: 'selectedDocumentsChanged', selectedDocuments });
  }, [selectedDocuments]);

  useEffect(() => {
    onSnapshot?.(state);
  }, [onSnapshot, state]);

  useImperativeHandle(
    ref,
    () => ({
      reset: () => dispatch({ type: 'newConversationStarted' }),
      hydrate: (sessionId, turns) =>
        dispatch({ type: 'conversationHydrated', sessionId, turns }),
      fail: (message) => dispatch({ type: 'requestFailed', message }),
      dismissError: () => dispatch({ type: 'requestErrorDismissed' }),
      getSnapshot: () => state,
    }),
    [state]
  );

  const send = useCallback(
    async (content: string) => {
      const userTurnId = generateTurnId('user');
      dispatch({
        type: 'userTurnSubmitted',
        id: userTurnId,
        content,
      });

      let assistantTurnId: string | null = null;
      let sessionId = state.conversationSessionId;
      let toolsUsed: UserConversationToolUse[] = [];
      let answerContent = '';
      let streamReportedError = false;
      let streamCompleted = false;

      const request = {
        content,
        tools: selectedTools,
        jobIds: selectedDocuments,
        sessionId,
        authToken,
      };

      try {
        await sendLlmChatStreamWithUnifiedTools({
          ...request,
          onEvent: (event, payload) => {
            const data = payloadRecord(payload);
            if (event === 'assistant_message_started') {
              if (typeof data.message_id !== 'string' || !data.message_id) {
                data.message_id = generateTurnId('assistant');
              }
              assistantTurnId = data.message_id as string;
            }
            if (typeof data.session_id === 'string' && data.session_id) {
              sessionId = data.session_id;
            }
            if (event === 'done') {
              streamCompleted = true;
              toolsUsed = readToolUses(data.tools_used);
            }
            if (event === 'answer_delta' && typeof data.delta === 'string') {
              answerContent += data.delta;
            }
            if (event === 'error') {
              streamReportedError = true;
              throw new Error(
                providerStreamErrorMessage(
                  typeof data.detail === 'string' ? data.detail : payload,
                  t('errors.failedToSendMessage')
                )
              );
            }
            const action = adaptSageStreamEvent(event, data, assistantTurnId);
            if (action) dispatch(action);
          },
        });

        if (!streamCompleted) {
          throw new Error(t('errors.failedToSendMessage'));
        }

        dispatch({ type: 'assistantTurnFinished', sessionId });
        if (assistantTurnId) {
          onTerminalTurn?.({
            userTurnId,
            assistantTurnId,
            sessionId,
            toolsUsed,
          });
        }
      } catch (error) {
        const message =
          error instanceof Error
            ? error.message
            : t('errors.failedToSendMessage');

        if (assistantTurnId && answerContent.trim()) {
          dispatch({
            type: 'assistantTurnFailed',
            assistantTurnId,
            message,
          });
          return;
        }

        if (streamReportedError) {
          if (assistantTurnId) {
            dispatch({
              type: 'assistantTurnFailed',
              assistantTurnId,
              message,
            });
          } else {
            dispatch({ type: 'requestFailed', message });
          }
          return;
        }

        if (assistantTurnId) {
          dispatch({
            type: 'assistantTurnFailed',
            assistantTurnId,
            message,
          });
          dispatch({ type: 'requestErrorDismissed' });
        }

        try {
          const response = await sendLlmChatWithUnifiedTools(request);
          if (response.status === 401 || response.status === 403) {
            onAuthFailure?.(response.status);
            dispatch({ type: 'assistantTurnFinished' });
            return;
          }
          if (!response.ok) {
            throw new Error(await responseErrorMessage(response));
          }
          const data = (await response.json()) as Record<string, unknown>;
          const fallbackAssistantTurnId =
            typeof data.message_id === 'string' && data.message_id
              ? data.message_id
              : generateTurnId('assistant');
          sessionId =
            typeof data.session_id === 'string' && data.session_id
              ? data.session_id
              : sessionId;
          toolsUsed = readToolUses(data.tools_used);
          dispatch({
            type: 'assistantTurnCompleted',
            id: fallbackAssistantTurnId,
            content: typeof data.message === 'string' ? data.message : '',
            trace: (data.trace as ConversationTrace | null | undefined) ?? null,
            sessionId,
          });
          onTerminalTurn?.({
            userTurnId,
            assistantTurnId: fallbackAssistantTurnId,
            sessionId,
            toolsUsed,
          });
        } catch (fallbackError) {
          dispatch({
            type: 'requestFailed',
            message:
              fallbackError instanceof Error ? fallbackError.message : message,
          });
        }
      }
    },
    [
      authToken,
      onAuthFailure,
      onTerminalTurn,
      selectedDocuments,
      selectedTools,
      state,
      t,
    ]
  );

  return (
    <ConversationSurface
      activityAudience="user"
      turns={buildConversationSurfaceTurns(state.turns)}
      onSend={(content) => void send(content)}
      isRunning={state.isRunning}
      placeholder={placeholder}
      toolbar={toolbar}
      notices={
        <div className="mt-4 space-y-2">
          {state.error && (
            <Callout
              label={t('chat.errors.requestLabel', 'Chat request error')}
              tone="error"
              className="flex items-center gap-3 animate-fade-in shadow-sm"
            >
              <AlertCircle
                className="h-4 w-4 shrink-0 text-error"
                aria-hidden="true"
              />
              <span className="flex-1">{state.error}</span>
              <IconButton
                label={t('common.close', 'Close')}
                onClick={() => dispatch({ type: 'requestErrorDismissed' })}
                variant="ghost"
                size="sm"
                className="text-error hover:bg-error/10"
              >
                <X className="h-4 w-4" aria-hidden="true" />
              </IconButton>
            </Callout>
          )}
          {notices}
        </div>
      }
      hasPersistedSession={Boolean(state.conversationSessionId)}
    />
  );
});
