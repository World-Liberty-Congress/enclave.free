import {
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { useTranslation } from 'react-i18next';
import {
  BookOpen,
  Database,
  LifeBuoy,
  X,
  MessageCircle,
  RefreshCw,
  Search,
  Settings2,
  ShieldAlert,
} from 'lucide-react';
import { adminFetch } from '../../utils/adminApi';
import {
  notifyAdminConfigChanged,
  readAdminConfigAffectedAreas,
} from '../../utils/adminConfigEvents';
import { ChatInput } from '../chat/ChatInput';
import {
  ChatMessage,
  type ConversationTraceDelta,
  type Message,
} from '../chat/ChatMessage';
import { readTraceDelta } from '../chat/SageStreamEventAdapter';
import { ToolSelector, type Tool } from '../chat/ToolSelector';
import { redactSecrets } from '../../utils/secretRedaction';
import { refreshAdminConfigRedactionMetadata } from '../../utils/adminConfigContext';
import { recordProviderFailureInstrumentation } from '../../utils/adminResilienceInstrumentation';
import { mergeTraceDeltas } from '../../utils/conversationTraceDeltas';
import {
  sendLlmChatStreamWithUnifiedTools,
  sendLlmChatWithUnifiedTools,
} from '../../utils/llmChat';
import {
  classifyProviderError,
  formatClassifiedProviderError,
  shouldOfferNewAssistantConversation,
  type ClassifiedProviderError,
} from '../../utils/providerErrors';

interface AdminConfigAssistantProps {
  variant?: 'sidebar' | 'drawer';
  /**
   * 'admin-config' (default) is the reactive config assistant.
   * 'onboarding' drives a guided first-run setup: it seeds a welcome opener while
   * Sage uses the same Admin Config Tool Set available in normal admin chat.
   */
  purpose?: 'admin-config' | 'onboarding';
  onCollapse?: () => void;
  onClose?: () => void;
  collapseIcon?: ReactNode;
}

const CONFIG_TOOL_ID = 'admin-config';
const KNOWLEDGE_TOOL_ID = 'knowledge-search';
const CURATED_RESOURCES_TOOL_ID = 'curated-resources';
export const ONBOARDING_WELCOME_MESSAGE =
  "Welcome — let's set up your space. Answer as many of these as you like in one message (number them, and skip anything you're unsure about):\n\n1. **Name** — what should we call this space? (shows in the header & browser tab)\n2. **Description** — one sentence on what it's for (a private note, not shown to users)\n3. **Assistant name** — what should the AI helper be called?\n4. **Accent color** — the highlight color for buttons & links (a name like blue/teal, or a hex like #3B82F6)\n5. **Theme** — light, dark, or system (match the device)?\n6. **Default language** — e.g. English or Spanish (optional)\n7. **Tagline** — a short line for the header (optional)\n8. **New users** — let them in right away, or approve each person?\n9. **User types** — what kinds of non-admin people will use this? (e.g. Teacher and Student) — I'll create each one for you\n\nExample: \"9. Teacher and Student\". I'll save everything in one step — and you can switch to manual setup anytime.";

// How often (in ms) streamed tokens are flushed to the message list while the
// assistant is responding. ~30 fps reads as smooth to the eye while avoiding a
// full re-render + syntax-highlight pass on every single token (which pegged the
// CPU and locked the UI). Tune here if needed.
const STREAM_FLUSH_INTERVAL_MS = 33;

function generateMessageId() {
  return `admin-msg-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

function createOnboardingMessage(content: string): Message {
  return {
    id: generateMessageId(),
    role: 'assistant',
    content,
    timestamp: new Date(),
  };
}

function patchAssistantMessage(
  messages: Message[],
  id: string,
  patch: Partial<Message>
): Message[] {
  return messages.map((message) =>
    message.id === id ? { ...message, ...patch } : message
  );
}

function appendAssistantTraceDelta(
  messages: Message[],
  id: string,
  traceDelta: ConversationTraceDelta
): Message[] {
  return messages.map((message) => {
    if (message.id !== id) return message;

    return {
      ...message,
      traceDeltas: mergeTraceDeltas(message.traceDeltas ?? [], [traceDelta]),
    };
  });
}

async function readErrorDetail(res: Response): Promise<string> {
  let detail = `HTTP ${res.status}`;
  try {
    const payload = await res.json();
    if (payload?.detail !== undefined) {
      detail =
        typeof payload.detail === 'string'
          ? payload.detail
          : JSON.stringify(payload.detail);
    }
  } catch {
    // ignore
  }
  return detail;
}

export function AdminConfigAssistant({
  variant = 'sidebar',
  purpose = 'admin-config',
  onCollapse,
  onClose,
  collapseIcon,
}: AdminConfigAssistantProps) {
  const { t } = useTranslation();
  const isOnboarding = purpose === 'onboarding';
  const [shareSecrets, setShareSecrets] = useState(false);
  const [messages, setMessages] = useState<Message[]>(() =>
    isOnboarding
      ? [
          createOnboardingMessage(
            t(
              'admin.configAssistant.onboardingWelcome',
              ONBOARDING_WELCOME_MESSAGE
            )
          ),
        ]
      : []
  );
  const [conversationSessionId, setConversationSessionId] = useState<
    string | null
  >(null);
  const [isLoading, setIsLoading] = useState(false);
  const [isRefreshLoading, setIsRefreshLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [recoveryError, setRecoveryError] =
    useState<ClassifiedProviderError | null>(null);
  const [snapshotInfo, setSnapshotInfo] = useState<{
    generatedAtIso: string;
  } | null>(null);
  const [selectedTools, setSelectedTools] = useState<string[]>([
    CONFIG_TOOL_ID,
  ]);

  const activeTurnRef = useRef<AbortController | null>(null);
  useEffect(
    () => () => {
      activeTurnRef.current?.abort();
      activeTurnRef.current = null;
    },
    []
  );
  const secretsForRedactionRef = useRef<string[]>([]);
  const messagesContainerRef = useRef<HTMLDivElement>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to the latest message. Always scroll when the operator just sent
  // (the newest message is theirs); otherwise only follow the stream if they're
  // already near the bottom, so scrolling up to read history isn't interrupted.
  useEffect(() => {
    const container = messagesContainerRef.current;
    const last = messages[messages.length - 1];
    const nearBottom = container
      ? container.scrollHeight - container.scrollTop - container.clientHeight <
        160
      : true;
    if (last?.role === 'user' || nearBottom) {
      messagesEndRef.current?.scrollIntoView({ block: 'end' });
    }
  }, [messages, isLoading]);

  const availableTools = useMemo<Tool[]>(() => {
    if (isOnboarding) {
      return [
        {
          id: CONFIG_TOOL_ID,
          name: t('chat.tools.configName', 'Config'),
          description: t(
            'chat.tools.config',
            'Read and update admin configuration'
          ),
          icon: <Settings2 className="h-3.5 w-3.5" aria-hidden="true" />,
        },
      ];
    }

    return [
      {
        id: KNOWLEDGE_TOOL_ID,
        name: t('chat.tools.knowledgeSearchName', 'Knowledge'),
        description: t(
          'chat.tools.knowledgeSearch',
          'Search uploaded documents and knowledge chunks'
        ),
        icon: <BookOpen className="h-3.5 w-3.5" aria-hidden="true" />,
      },
      {
        id: CURATED_RESOURCES_TOOL_ID,
        name: t('chat.tools.curatedResourcesName', 'Resources'),
        description: t(
          'chat.tools.curatedResources',
          'Find admin-vetted referral resources and trusted organizations'
        ),
        icon: <LifeBuoy className="h-3.5 w-3.5" aria-hidden="true" />,
      },
      {
        id: 'web-search',
        name: t('chat.tools.webSearchName'),
        description: t('chat.tools.webSearch'),
        icon: <Search className="h-3.5 w-3.5" aria-hidden="true" />,
      },
      {
        id: CONFIG_TOOL_ID,
        name: t('chat.tools.configName', 'Config'),
        description: t(
          'chat.tools.config',
          'Read and update admin configuration'
        ),
        icon: <Settings2 className="h-3.5 w-3.5" aria-hidden="true" />,
      },
      {
        id: 'db-query',
        name: t('chat.tools.databaseName'),
        description: t('chat.tools.database'),
        icon: <Database className="h-3.5 w-3.5" aria-hidden="true" />,
      },
    ];
  }, [isOnboarding, t]);

  const fetchJson = useCallback(
    async <T,>(endpoint: string, options?: RequestInit): Promise<T> => {
      const res = await adminFetch(endpoint, options);
      if (!res.ok) {
        throw new Error(await readErrorDetail(res));
      }
      return res.json() as Promise<T>;
    },
    []
  );

  const hasConfigTool = selectedTools.includes(CONFIG_TOOL_ID);

  const handleToolToggle = useCallback(
    (toolId: string) => {
      // During onboarding the toolset is a locked, injected dependency: the
      // assistant must stay in config-only mode (no web/database). Ignore any
      // toggle attempts so web/database can never be enabled mid-onboarding.
      if (isOnboarding || toolId === CONFIG_TOOL_ID) {
        return;
      }
      setSelectedTools((prev) =>
        prev.includes(toolId)
          ? prev.filter((id) => id !== toolId)
          : [...prev, toolId]
      );
    },
    [selectedTools, isOnboarding]
  );

  const handleStartNewAssistantConversation = useCallback(() => {
    activeTurnRef.current?.abort();
    activeTurnRef.current = null;
    setIsLoading(false);
    setConversationSessionId(null);
    setMessages(
      isOnboarding
        ? [
            createOnboardingMessage(
              t(
                'admin.configAssistant.onboardingWelcome',
                ONBOARDING_WELCOME_MESSAGE
              )
            ),
          ]
        : []
    );
    setError(null);
    setRecoveryError(null);
    setShareSecrets(false);
    secretsForRedactionRef.current = [];
  }, [isOnboarding, t]);

  const handleSend = useCallback(
    async (content: string) => {
      activeTurnRef.current?.abort();
      const turnController = new AbortController();
      activeTurnRef.current = turnController;
      const userMessage: Message = {
        id: generateMessageId(),
        role: 'user',
        content,
        timestamp: new Date(),
      };
      setMessages((prev) => [...prev, userMessage]);
      setIsLoading(true);
      setError(null);
      setRecoveryError(null);

      try {
        if (!hasConfigTool) {
          setSnapshotInfo(null);
        }

        let streamed = false;
        let streamMessageId: string | null = null;
        let raw = '';
        let streamSessionId: string | null = null;
        let streamReportedError = false;
        let classifiedStreamError: ClassifiedProviderError | null = null;
        let lastDeltaFlushAt = 0;
        const notifiedAffectedAreas = new Set<string>();
        const notifyAffectedAreas = (payload: unknown) => {
          const freshAreas = readAdminConfigAffectedAreas(payload).filter(
            (area) => !notifiedAffectedAreas.has(area)
          );
          freshAreas.forEach((area) => notifiedAffectedAreas.add(area));
          notifyAdminConfigChanged(freshAreas);
        };

        // Compute the display string from the accumulated stream so far.
        const computeStreamDisplay = () =>
          shareSecrets
            ? redactSecrets(raw, secretsForRedactionRef.current)
            : raw;
        try {
          await sendLlmChatStreamWithUnifiedTools({
            signal: turnController.signal,
            content,
            tools: selectedTools,
            conversationSurface: isOnboarding ? 'admin-onboarding' : undefined,
            sessionId: conversationSessionId,
            adminUserLookup: selectedTools.includes('db-query'),
            onEvent: (event, payload) => {
              if (turnController.signal.aborted) return;
              const data = payload as Record<string, unknown>;
              notifyAffectedAreas(data);
              if (event === 'assistant_message_started') {
                const assistantId =
                  typeof data.message_id === 'string'
                    ? data.message_id
                    : generateMessageId();
                streamMessageId = assistantId;
                if (typeof data.session_id === 'string')
                  streamSessionId = data.session_id;
                setMessages((prev) => [
                  ...prev,
                  {
                    id: assistantId,
                    role: 'assistant',
                    content: '',
                    timestamp: new Date(),
                    traceStatus: t(
                      'chat.trace.finalizing',
                      'Finalizing response...'
                    ),
                  },
                ]);
              } else if (event === 'trace_status' && streamMessageId) {
                const status =
                  typeof data.status === 'string'
                    ? data.status
                    : t('chat.trace.finalizing', 'Finalizing response...');
                setMessages((prev) =>
                  patchAssistantMessage(prev, streamMessageId!, {
                    traceStatus: status,
                  })
                );
              } else if (event === 'trace_delta' && streamMessageId) {
                const traceDelta = readTraceDelta(data);
                if (traceDelta) {
                  setMessages((prev) =>
                    appendAssistantTraceDelta(
                      prev,
                      streamMessageId!,
                      traceDelta
                    )
                  );
                }
              } else if (event === 'answer_delta' && streamMessageId) {
                const delta = typeof data.delta === 'string' ? data.delta : '';
                raw += delta;
                // Throttle re-renders: flush at most once per
                // STREAM_FLUSH_INTERVAL_MS instead of on every token. The final
                // flush after the stream ends renders any buffered remainder.
                const now = performance.now();
                if (now - lastDeltaFlushAt >= STREAM_FLUSH_INTERVAL_MS) {
                  lastDeltaFlushAt = now;
                  const display = computeStreamDisplay();
                  setMessages((prev) =>
                    patchAssistantMessage(prev, streamMessageId!, {
                      content: display,
                    })
                  );
                }
              } else if (event === 'trace_final' && streamMessageId) {
                setMessages((prev) =>
                  patchAssistantMessage(prev, streamMessageId!, {
                    trace: data.trace as Message['trace'],
                    traceStatus: null,
                  })
                );
              } else if (event === 'done') {
                if (typeof data.session_id === 'string')
                  streamSessionId = data.session_id;
              } else if (event === 'error') {
                streamReportedError = true;
                const classified = classifyProviderError(
                  typeof data.detail === 'string' ? data.detail : data
                );
                classifiedStreamError = classified;
                recordProviderFailureInstrumentation({
                  surface: 'admin_config_assistant',
                  classified,
                });
                throw new Error(
                  classified.category === 'unknown' &&
                    typeof data.detail !== 'string'
                    ? t('errors.failedToSendMessage')
                    : formatClassifiedProviderError(classified)
                );
              }
            },
          });
          if (streamSessionId) {
            setConversationSessionId(streamSessionId);
          }
          streamed = true;
        } catch (streamError) {
          if (
            activeTurnRef.current !== turnController ||
            turnController.signal.aborted
          )
            return;
          if (streamMessageId && raw.trim()) {
            setError(
              streamError instanceof Error
                ? streamError.message
                : t('errors.failedToSendMessage')
            );
            if (
              classifiedStreamError &&
              shouldOfferNewAssistantConversation(classifiedStreamError)
            ) {
              setRecoveryError(classifiedStreamError);
            }
            return;
          }
          if (streamMessageId) {
            setMessages((prev) =>
              prev.filter((message) => message.id !== streamMessageId)
            );
          }
          if (streamReportedError) {
            setError(
              streamError instanceof Error
                ? streamError.message
                : t('errors.failedToSendMessage')
            );
            if (
              classifiedStreamError &&
              shouldOfferNewAssistantConversation(classifiedStreamError)
            ) {
              setRecoveryError(classifiedStreamError);
            }
            return;
          }
          if (
            streamError &&
            typeof streamError === 'object' &&
            'streamStarted' in streamError &&
            streamError.streamStarted
          ) {
            throw streamError;
          }
          if (selectedTools.includes(CONFIG_TOOL_ID)) {
            throw streamError;
          }
          console.warn(
            'Streaming admin assistant failed; falling back to non-streaming chat:',
            streamError
          );
        } finally {
          if (streamMessageId && activeTurnRef.current === turnController) {
            const assistantId = streamMessageId;
            const display = computeStreamDisplay();
            setMessages((prev) =>
              patchAssistantMessage(prev, assistantId, {
                content: display,
                traceStatus: null,
              })
            );
          }
        }

        if (!streamed) {
          const res = await sendLlmChatWithUnifiedTools({
            signal: turnController.signal,
            content,
            tools: selectedTools,
            conversationSurface: isOnboarding ? 'admin-onboarding' : undefined,
            sessionId: conversationSessionId,
            adminUserLookup: selectedTools.includes('db-query'),
          });
          if (res.status === 401) {
            window.location.href = '/admin';
            return;
          }
          if (!res.ok) {
            const detail = await readErrorDetail(res);
            const classified = classifyProviderError(detail);
            recordProviderFailureInstrumentation({
              surface: 'admin_config_assistant',
              classified,
            });
            if (shouldOfferNewAssistantConversation(classified)) {
              setRecoveryError(classified);
            }
            throw new Error(formatClassifiedProviderError(classified));
          }
          const data = (await res.json()) as {
            message?: string;
            message_id?: string;
            session_id?: string;
            trace?: Message['trace'];
            admin_config_affected_areas?: unknown;
          };
          notifyAffectedAreas(data);
          if (data.session_id) {
            setConversationSessionId(data.session_id);
          }
          raw = String(data?.message || '');

          const assistantId = data.message_id || generateMessageId();

          const display = shareSecrets
            ? redactSecrets(raw, secretsForRedactionRef.current)
            : raw;

          const assistantMessage: Message = {
            id: assistantId,
            role: 'assistant',
            content: display,
            timestamp: new Date(),
            trace: data.trace ?? null,
          };
          setMessages((prev) => [...prev, assistantMessage]);
        }
      } catch (e) {
        if (turnController.signal.aborted) return;
        setError(
          e instanceof Error ? e.message : t('errors.failedToSendMessage')
        );
      } finally {
        turnController.abort();
        if (activeTurnRef.current === turnController) {
          activeTurnRef.current = null;
          setIsLoading(false);
        }
      }
    },
    [
      conversationSessionId,
      hasConfigTool,
      isOnboarding,
      selectedTools,
      shareSecrets,
      t,
    ]
  );

  const closeDrawer = () => {
    setError(null);
    // Secrets are opt-in and should not persist beyond the session UI.
    setShareSecrets(false);
    secretsForRedactionRef.current = [];
    onClose?.();
  };

  const inputToolbar = (
    <ToolSelector
      tools={availableTools}
      selectedTools={selectedTools}
      onToggle={handleToolToggle}
      disabledToolIds={[CONFIG_TOOL_ID]}
      compact
    />
  );

  const containerClass =
    variant === 'drawer'
      ? 'ml-auto h-full w-[min(92vw,400px)] bg-surface border-l border-border shadow-2xl'
      : 'h-full w-full bg-surface';

  return (
    <div className={`${containerClass} overflow-hidden flex flex-col`}>
      <div className="px-4 py-3 border-b border-border bg-surface-raised flex items-center justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <MessageCircle className="w-4 h-4 text-accent" />
            <div className="font-semibold text-text truncate">
              {isOnboarding
                ? t(
                    'admin.configAssistant.onboardingTitle',
                    'Guided Setup Chat'
                  )
                : t('admin.configAssistant.title')}
            </div>
          </div>
          <div className="text-xs text-text-muted mt-0.5">
            {isOnboarding
              ? t(
                  'admin.configAssistant.onboardingContext',
                  'Focused setup conversation'
                )
              : !hasConfigTool
                ? t('admin.configAssistant.contextToolOff')
                : snapshotInfo?.generatedAtIso
                  ? t('admin.configAssistant.contextReady', {
                      timestamp: new Date(
                        snapshotInfo.generatedAtIso
                      ).toLocaleString(),
                    })
                  : t('admin.configAssistant.contextNotLoaded')}
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {!isOnboarding && (
            <button
              onClick={async () => {
                if (!hasConfigTool) return;
                setError(null);
                setIsRefreshLoading(true);
                try {
                  const metadata = await refreshAdminConfigRedactionMetadata({
                    shareSecrets,
                    fetchJson,
                  });
                  setSnapshotInfo({ generatedAtIso: new Date().toISOString() });
                  secretsForRedactionRef.current = metadata.secretValues;
                } catch (e) {
                  setError(
                    e instanceof Error
                      ? e.message
                      : t('admin.configAssistant.refreshFailed')
                  );
                } finally {
                  setIsRefreshLoading(false);
                }
              }}
              className="p-2 rounded-xl hover:bg-surface-overlay text-text-muted hover:text-text transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              title={t('admin.configAssistant.refreshContext')}
              aria-label={t('admin.configAssistant.refreshContext')}
              disabled={!hasConfigTool || isRefreshLoading}
            >
              <RefreshCw
                className={`w-4 h-4 ${isRefreshLoading ? 'animate-spin' : ''}`}
              />
            </button>
          )}
          {variant === 'sidebar' && onCollapse && (
            <button
              onClick={onCollapse}
              className="p-2 rounded-xl hover:bg-surface-overlay text-text-muted hover:text-text transition-colors"
              title={t(
                'admin.configAssistant.collapseSidebar',
                'Collapse assistant sidebar'
              )}
              aria-label={t(
                'admin.configAssistant.collapseSidebar',
                'Collapse assistant sidebar'
              )}
            >
              {collapseIcon}
            </button>
          )}
          {variant === 'drawer' && (
            <button
              onClick={closeDrawer}
              className="p-2 rounded-xl hover:bg-surface-overlay text-text-muted hover:text-text transition-colors"
              title={t('common.close')}
              aria-label={t('common.close')}
            >
              <X className="w-4 h-4" />
            </button>
          )}
        </div>
      </div>

      {!isOnboarding && (
        <div className="px-4 py-3 border-b border-border bg-surface flex items-start justify-between gap-3">
          <label
            className={`flex items-start gap-3 select-none ${hasConfigTool ? 'cursor-pointer' : 'cursor-not-allowed opacity-60'}`}
            aria-disabled={!hasConfigTool}
          >
            <input
              type="checkbox"
              checked={shareSecrets}
              disabled={!hasConfigTool}
              onChange={async (e) => {
                const checked = e.target.checked;
                setShareSecrets(checked);
                if (!checked) {
                  secretsForRedactionRef.current = [];
                  return;
                }
                try {
                  const metadata = await refreshAdminConfigRedactionMetadata({
                    shareSecrets: true,
                    fetchJson,
                  });
                  secretsForRedactionRef.current = metadata.secretValues;
                } catch {
                  secretsForRedactionRef.current = [];
                }
              }}
              className="mt-1 disabled:cursor-not-allowed"
            />
            <div>
              <div className="text-sm font-medium text-text">
                {t('admin.configAssistant.shareSecretsTitle')}
              </div>
              <div className="text-xs text-text-muted">
                {t(
                  'admin.configAssistant.directShareSecretsHint',
                  "Optional: include stored deployment secret values in this Conversation's context. Direct secret reads do not require this."
                )}
              </div>
            </div>
          </label>
          {hasConfigTool && shareSecrets && (
            <div className="flex items-center gap-2 text-xs text-warning shrink-0">
              <ShieldAlert className="w-4 h-4" />
              <span className="hidden sm:inline">
                {t('admin.configAssistant.sensitive')}
              </span>
            </div>
          )}
        </div>
      )}

      <div
        ref={messagesContainerRef}
        className="flex-1 overflow-y-auto px-3 py-4"
      >
        <div className="space-y-4">
          {messages.length === 0 ? (
            <div className="text-sm text-text-muted">
              {t(
                'admin.configAssistant.directWritePrompt',
                'Ask about admin configuration. Sage can inspect settings and, after confirming with you in the conversation, apply supported changes directly.'
              )}
            </div>
          ) : (
            messages.map((m) => (
              // Admin Conversations keep Activity expanded; it is collapsed by
              // default for Users. See #636.
              <ChatMessage
                key={m.id}
                message={m}
                activityAudience="admin"
                defaultActivityOpen
              />
            ))
          )}

          {isLoading && (
            <div className="animate-fade-in-up">
              <div className="flex gap-3">
                <div className="w-7 h-7 rounded-full bg-gradient-to-br from-accent to-accent-hover flex items-center justify-center shrink-0 shadow-md ring-1 ring-white/10">
                  <MessageCircle className="w-3.5 h-3.5 text-white" />
                </div>
                <div className="flex items-center gap-2 px-4 py-3 bg-surface-raised border border-border rounded-2xl rounded-bl-md">
                  <div className="flex items-center gap-1">
                    <span className="w-2 h-2 bg-accent/60 rounded-full typing-dot" />
                    <span className="w-2 h-2 bg-accent/60 rounded-full typing-dot" />
                    <span className="w-2 h-2 bg-accent/60 rounded-full typing-dot" />
                  </div>
                  <span className="text-sm text-text-secondary animate-pulse-subtle">
                    {t('chat.typing')}
                  </span>
                </div>
              </div>
            </div>
          )}

          {error && (
            <div className="text-sm text-error bg-error/10 border border-error/20 rounded-xl px-3 py-2 space-y-2">
              <div>{error}</div>
              {recoveryError &&
                shouldOfferNewAssistantConversation(recoveryError) && (
                  <button
                    type="button"
                    onClick={handleStartNewAssistantConversation}
                    className="inline-flex items-center gap-2 px-3 py-2 rounded-xl bg-surface-raised border border-border text-text hover:bg-surface-overlay transition-colors text-sm font-medium"
                  >
                    {t('admin.configAssistant.startNewConversation')}
                  </button>
                )}
            </div>
          )}

          <div ref={messagesEndRef} />
        </div>
      </div>

      <ChatInput
        onSend={(msg) => void handleSend(msg)}
        disabled={isLoading}
        onStop={isLoading ? () => activeTurnRef.current?.abort() : undefined}
        placeholder={t('admin.configAssistant.inputPlaceholder')}
        toolbar={isOnboarding ? undefined : inputToolbar}
      />
    </div>
  );
}
