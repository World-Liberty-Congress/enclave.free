import { ReactNode, useCallback, useMemo, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import {
  AssistantRuntimeProvider,
  useExternalStoreRuntime,
  type AppendMessage,
} from '@assistant-ui/react';
import { AssistantConversationThread } from './AssistantConversationThread';
import {
  buildAssistantConversationState,
  extractAppendMessageText,
} from './AssistantTurnAdapter';
import { AssistantComposerInput } from './AssistantComposerInput';
import type { Message } from './ChatMessage';
import type { ConversationActivityAudience } from './conversationActivityPresentation';
import type {
  ConversationMessageActionId,
  ConversationTransportCapabilities,
} from './ConversationMessageActions';
import type { ConversationSurfaceTurn } from './ConversationSurfaceModel';

let conversationSurfaceRuntimeInstance = 0;

interface ConversationSurfaceProps {
  turns: ConversationSurfaceTurn[];
  onSend: (message: string) => void;
  onStop?: () => void;
  activityAudience: ConversationActivityAudience;
  isRunning?: boolean;
  disabled?: boolean;
  placeholder?: string;
  toolbar?: ReactNode;
  turnAccessories?: Record<string, ReactNode>;
  notices?: ReactNode;
  transportCapabilities?: ConversationTransportCapabilities;
  hasPersistedSession?: boolean;
  hasPendingApproval?: boolean;
  /**
   * Admin surfaces pass true so Activity starts expanded. Users get the
   * collapsed default; see #636.
   */
  defaultActivityOpen?: boolean;
  onMessageAction?: (
    actionId: ConversationMessageActionId,
    message: Message
  ) => void;
}

export function ConversationSurface({
  turns,
  onSend,
  onStop,
  activityAudience,
  isRunning = false,
  disabled = false,
  placeholder,
  toolbar,
  turnAccessories,
  notices,
  transportCapabilities,
  hasPersistedSession = false,
  hasPendingApproval = false,
  defaultActivityOpen = false,
  onMessageAction,
}: ConversationSurfaceProps) {
  const { t } = useTranslation();
  const runtimeMessageIdPrefix = useRef(
    `surface-${++conversationSurfaceRuntimeInstance}-`
  ).current;
  const assistantState = useMemo(
    () =>
      buildAssistantConversationState({
        turns,
        isRunning,
        disabled,
        turnAccessories,
        transportCapabilities,
        hasPersistedSession,
        hasPendingApproval,
        runtimeMessageIdPrefix,
        translate: t,
      }),
    [
      disabled,
      hasPendingApproval,
      hasPersistedSession,
      isRunning,
      runtimeMessageIdPrefix,
      transportCapabilities,
      turnAccessories,
      turns,
      t,
    ]
  );
  const handleNew = useCallback(
    async (message: AppendMessage) => {
      const text = extractAppendMessageText(message).trim();
      if (text) onSend(text);
    },
    [onSend]
  );
  const runtime = useExternalStoreRuntime({
    messages: assistantState.messages,
    isRunning: assistantState.isRunning,
    isDisabled: assistantState.isDisabled,
    onNew: handleNew,
  });

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <section
        aria-label={t('chat.conversationSurfaceAria', 'Conversation surface')}
        className="flex min-h-0 flex-1 flex-col"
      >
        <AssistantConversationThread
          assistantState={assistantState}
          runningLabel={t('chat.typing')}
          activityAudience={activityAudience}
          defaultActivityOpen={defaultActivityOpen}
          notices={notices}
          onMessageAction={onMessageAction}
        />
        <AssistantComposerInput
          disabled={disabled || isRunning}
          onStop={isRunning ? onStop : undefined}
          placeholder={placeholder}
          toolbar={toolbar}
        />
      </section>
    </AssistantRuntimeProvider>
  );
}
