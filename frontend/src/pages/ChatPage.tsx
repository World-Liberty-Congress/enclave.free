import {
  useState,
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  type FormEvent,
} from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  AlertCircle,
  BookOpen,
  CheckCircle2,
  Database,
  EyeOff,
  LifeBuoy,
  Mail,
  Pencil,
  Plus,
  Search,
  Settings2,
  MessageSquare,
  Trash2,
  X,
} from 'lucide-react';
import { ChatContainer } from '../components/chat/ChatContainer';
import { ConversationSurface } from '../components/chat/ConversationSurface';
import { buildConversationSurfaceTurns } from '../components/chat/ConversationSurfaceModel';
import {
  adaptSageStreamEvent,
  readTraceDelta,
  createConversationUiState,
  reduceConversationUiState,
  UserConversation,
  type UserConversationHandle,
  type ConversationUiState,
  type ConversationUiTurn,
} from '../components/chat';
import { ToolSelector, Tool } from '../components/chat/ToolSelector';
import {
  DocumentScope,
  DocumentSource,
} from '../components/chat/DocumentScope';
import { ExportButton } from '../components/chat/ExportButton';
import { AppHeader } from '../components/shared/AppHeader';
import { LanguageSwitcher } from '../components/onboarding/LanguageSwitcher';
import { Message } from '../components/chat/ChatMessage';
import type {
  ConversationActivityStep,
  ConversationTraceDelta,
  ConversationTrace,
} from '../components/chat/ChatMessage';
import {
  ReachoutModal,
  type ReachoutMode,
} from '../components/reachout/ReachoutModal';
import {
  API_BASE,
  STORAGE_KEYS,
  getSelectedUserTypeId,
  saveSelectedUserTypeId,
} from '../types/onboarding';
import {
  adminFetch,
  isAdminAuthenticated,
  validateAdminSession,
} from '../utils/adminApi';
import {
  notifyAdminConfigChanged,
  readAdminConfigAffectedAreas,
} from '../utils/adminConfigEvents';
import {
  sendLlmChatStreamWithUnifiedTools,
  sendLlmChatWithUnifiedTools,
} from '../utils/llmChat';
import { Button, Callout, IconButton } from '../components/ui';
import { redactSecrets } from '../utils/secretRedaction';
import {
  classifyProviderError,
  formatClassifiedProviderError,
  shouldOfferNewAssistantConversation,
} from '../utils/providerErrors';
import { recordProviderFailureInstrumentation } from '../utils/adminResilienceInstrumentation';
import { refreshAdminConfigRedactionMetadata } from '../utils/adminConfigContext';
import { resolveUserConversationSessionDefaults } from '../utils/sessionDefaults';

const CONFIG_TOOL_ID = 'admin-config';
const KNOWLEDGE_TOOL_ID = 'knowledge-search';
const CURATED_RESOURCES_TOOL_ID = 'curated-resources';
const ADMIN_ONLY_TOOL_IDS = new Set([CONFIG_TOOL_ID, 'db-query']);
export const ENCLAVE_USER_EMAIL_KEY = STORAGE_KEYS.USER_EMAIL;

type ChatAdminSessionState = 'checking' | 'authenticated' | 'unauthenticated';
const ADMIN_SESSION_VALIDATION_TIMEOUT_MS = 8000;

function withTimeout<T>(promise: Promise<T>, timeoutMs: number): Promise<T> {
  let timeoutId: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timeoutId = setTimeout(
      () => reject(new Error('Admin session validation timed out')),
      timeoutMs
    );
  });
  return Promise.race([promise, timeout]).finally(() => {
    if (timeoutId !== undefined) clearTimeout(timeoutId);
  });
}

function filterToolsForActor(tools: string[], isAdmin: boolean): string[] {
  if (isAdmin) return tools;
  return tools.filter((tool) => !ADMIN_ONLY_TOOL_IDS.has(tool));
}

function conversationTurnToMessage(turn: ConversationUiTurn): Message {
  return {
    id: turn.id,
    role: turn.role,
    content: turn.content,
    trace: turn.trace,
    traceStatus: turn.traceStatus,
    activitySteps: turn.activitySteps,
    traceDeltas: turn.traceDeltas,
    controlSnapshot: turn.controlSnapshot,
  };
}

interface ConversationHistorySummary {
  id: string;
  title: string;
  messageCount: number | null;
  updatedAt: string | null;
}

interface ConversationSessionView {
  id: string;
  title: string | null;
  turns: ConversationUiTurn[];
}

function conversationHistorySummaryFromApi(
  value: unknown
): ConversationHistorySummary | null {
  if (!value || typeof value !== 'object') return null;
  const record = value as Record<string, unknown>;
  if (typeof record.id !== 'string' || !record.id.trim()) return null;
  const rawTitle =
    typeof record.title === 'string' && record.title.trim()
      ? record.title.trim()
      : null;
  const messageCount =
    typeof record.message_count === 'number' &&
    Number.isFinite(record.message_count)
      ? Math.max(0, Math.floor(record.message_count))
      : null;
  const updatedAt =
    typeof record.updated_at === 'string' && record.updated_at.trim()
      ? record.updated_at
      : null;

  return {
    id: record.id,
    title: rawTitle ?? 'Untitled chat',
    messageCount,
    updatedAt,
  };
}

function conversationSessionViewFromApi(
  value: unknown
): ConversationSessionView | null {
  if (!value || typeof value !== 'object') return null;
  const record = value as Record<string, unknown>;
  if (typeof record.id !== 'string' || !record.id.trim()) return null;
  if (!Array.isArray(record.messages)) return null;
  const rawMessages = record.messages;
  return {
    id: record.id,
    title:
      typeof record.title === 'string' && record.title.trim()
        ? record.title.trim()
        : null,
    turns: rawMessages
      .map((message, index) =>
        conversationTurnFromSessionMessage(message, record.id as string, index)
      )
      .filter((turn): turn is ConversationUiTurn => Boolean(turn)),
  };
}

function conversationTurnFromSessionMessage(
  value: unknown,
  sessionId: string,
  index: number
): ConversationUiTurn | null {
  if (!value || typeof value !== 'object') return null;
  const record = value as Record<string, unknown>;
  if (record.role !== 'user' && record.role !== 'assistant') return null;
  if (typeof record.content !== 'string') return null;
  const trace = conversationTraceFromApi(record.trace);
  const activitySteps = activityStepsFromApi(record.activity_steps);
  const topLevelTraceDeltas = traceDeltasFromApi(record.trace_deltas);
  const traceDeltas = topLevelTraceDeltas.length
    ? topLevelTraceDeltas
    : traceDeltasFromApi(trace?.trace_deltas);
  return {
    id:
      typeof record.id === 'string' && record.id.trim()
        ? record.id
        : `${sessionId}-${index}`,
    role: record.role,
    content: record.content,
    activitySteps:
      activitySteps.length > 0
        ? activitySteps
        : trace?.activity_steps
          ? activityStepsFromApi(trace.activity_steps)
          : [],
    traceDeltas,
    trace,
    traceStatus: null,
  };
}

function conversationTraceFromApi(value: unknown): ConversationTrace | null {
  if (!value || typeof value !== 'object') return null;
  const record = value as Record<string, unknown>;
  if (
    !['off', 'minimal', 'summary', 'detailed'].includes(
      String(record.visibility)
    )
  ) {
    return null;
  }
  if (!isOptionalObjectArray(record.tools)) return null;
  if (!isOptionalObjectArray(record.retrieval)) {
    return null;
  }
  if (!isOptionalObjectArray(record.activity_steps)) {
    return null;
  }
  if (!isOptionalObjectArray(record.trace_deltas)) {
    return null;
  }
  return record as unknown as ConversationTrace;
}

function traceDeltasFromApi(value: unknown): ConversationTraceDelta[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item): ConversationTraceDelta | null => {
      if (!item || typeof item !== 'object') return null;
      return readTraceDelta({ trace_delta: item as Record<string, unknown> });
    })
    .filter((delta): delta is ConversationTraceDelta => Boolean(delta));
}

function isOptionalObjectArray(value: unknown): boolean {
  return (
    value === undefined ||
    (Array.isArray(value) &&
      value.every((item) => Boolean(item) && typeof item === 'object'))
  );
}

function activityStepsFromApi(value: unknown): ConversationActivityStep[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item): ConversationActivityStep | null => {
      if (!item || typeof item !== 'object') return null;
      const step = item as Record<string, unknown>;
      if (
        typeof step.id !== 'string' ||
        typeof step.kind !== 'string' ||
        typeof step.title !== 'string' ||
        typeof step.status !== 'string'
      ) {
        return null;
      }
      return {
        id: step.id,
        kind: step.kind,
        title: step.title,
        status: step.status,
        summary: typeof step.summary === 'string' ? step.summary : undefined,
        warnings: Array.isArray(step.warnings)
          ? step.warnings.filter(
              (warning): warning is string => typeof warning === 'string'
            )
          : undefined,
      };
    })
    .filter((step): step is ConversationActivityStep => Boolean(step));
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

function formatProviderStreamError(
  detail: unknown,
  fallbackMessage: string,
  options?: { recordAdminInstrumentation?: boolean }
): string {
  const classified = classifyProviderError(
    typeof detail === 'string' ? detail : detail
  );
  if (options?.recordAdminInstrumentation) {
    recordProviderFailureInstrumentation({
      surface: 'admin_chat_page',
      classified,
    });
  }
  if (classified.category === 'unknown' && typeof detail !== 'string') {
    return fallbackMessage;
  }
  return formatClassifiedProviderError(classified);
}

function formatProviderResponseError(
  detail: unknown,
  statusFallback: string,
  options?: { recordAdminInstrumentation?: boolean }
): string {
  const classified = classifyProviderError(detail);
  if (options?.recordAdminInstrumentation) {
    recordProviderFailureInstrumentation({
      surface: 'admin_chat_page',
      classified,
    });
  }
  if (classified.category === 'unknown' && !detail) {
    return statusFallback;
  }
  return formatClassifiedProviderError(classified);
}

export function ChatPage() {
  const navigate = useNavigate();
  const { t } = useTranslation();
  const [adminSessionState, setAdminSessionState] =
    useState<ChatAdminSessionState>(() =>
      isAdminAuthenticated() ? 'checking' : 'unauthenticated'
    );
  const isAdmin = adminSessionState === 'authenticated';
  const adminSessionChecking = adminSessionState === 'checking';
  const [conversationState, dispatchConversation] = useReducer(
    reduceConversationUiState,
    undefined,
    () =>
      createConversationUiState({
        selectedTools: [],
      })
  );
  const userConversationRef = useRef<UserConversationHandle>(null);
  const activeTurnRef = useRef<AbortController | null>(null);
  const cancelActiveTurn = useCallback(() => {
    activeTurnRef.current?.abort();
    activeTurnRef.current = null;
  }, []);
  useEffect(() => cancelActiveTurn, [cancelActiveTurn]);
  const resetAdminConversation = useCallback(() => {
    cancelActiveTurn();
    dispatchConversation({ type: 'newConversationStarted' });
  }, [cancelActiveTurn]);
  const [userConversationState, setUserConversationState] =
    useState<ConversationUiState>(() => createConversationUiState());
  const [userSelectedTools, setUserSelectedTools] = useState<string[]>([]);
  const [userSelectedDocuments, setUserSelectedDocuments] = useState<string[]>(
    []
  );
  const [documents, setDocuments] = useState<DocumentSource[]>([]);
  const [sessionDefaultsLoaded, setSessionDefaultsLoaded] = useState(false);
  const [pendingDefaultDocs, setPendingDefaultDocs] = useState<string[]>([]);
  const [shareSecrets, setShareSecrets] = useState(false);
  const [secretsForRedaction, setSecretsForRedaction] = useState<string[]>([]);
  const messages = useMemo(
    () =>
      (isAdmin ? conversationState.turns : userConversationState.turns).map(
        conversationTurnToMessage
      ),
    [conversationState.turns, isAdmin, userConversationState.turns]
  );
  const selectedTools = isAdmin
    ? conversationState.selectedTools
    : userSelectedTools;
  const actorScopedSelectedTools = useMemo(
    () => filterToolsForActor(selectedTools, isAdmin),
    [isAdmin, selectedTools]
  );
  const selectedDocuments = isAdmin
    ? conversationState.selectedDocuments
    : userSelectedDocuments;
  const conversationSessionId = isAdmin
    ? conversationState.conversationSessionId
    : userConversationState.conversationSessionId;
  const configToolEnabled = isAdmin && selectedTools.includes(CONFIG_TOOL_ID);
  const prevConversationSessionIdRef = useRef<string | null>(
    conversationSessionId
  );
  const isLoading = isAdmin
    ? conversationState.isRunning
    : userConversationState.isRunning;
  const error = isAdmin ? conversationState.error : userConversationState.error;
  useEffect(() => {
    let cancelled = false;

    if (!isAdminAuthenticated()) {
      setAdminSessionState('unauthenticated');
      return;
    }

    setAdminSessionState('checking');
    withTimeout(validateAdminSession(), ADMIN_SESSION_VALIDATION_TIMEOUT_MS)
      .then((state) => {
        if (cancelled) return;
        setAdminSessionState(
          state === 'authenticated' ? 'authenticated' : 'unauthenticated'
        );
      })
      .catch(() => {
        if (!cancelled) setAdminSessionState('unauthenticated');
      });

    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (adminSessionChecking || isAdmin) return;
    const filteredTools = filterToolsForActor(selectedTools, false);
    if (filteredTools.length !== selectedTools.length) {
      setUserSelectedTools(filteredTools);
    }
    setShareSecrets(false);
    setSecretsForRedaction([]);
  }, [adminSessionChecking, isAdmin, selectedTools]);

  useEffect(() => {
    const previousSessionId = prevConversationSessionIdRef.current;
    const sessionWasReset = conversationSessionId === null;
    const sessionWasSwapped =
      previousSessionId !== null && conversationSessionId !== previousSessionId;

    if (!configToolEnabled || sessionWasReset || sessionWasSwapped) {
      setShareSecrets(false);
      setSecretsForRedaction([]);
    }

    prevConversationSessionIdRef.current = conversationSessionId;
  }, [conversationSessionId, configToolEnabled]);

  const [reachoutOpen, setReachoutOpen] = useState(false);
  const [reachoutEnabled, setReachoutEnabled] = useState(false);
  const [reachoutMode, setReachoutMode] = useState<ReachoutMode>('support');
  const [reachoutOverrides, setReachoutOverrides] = useState<{
    title?: string;
    description?: string;
    buttonLabel?: string;
    successMessage?: string;
  }>({});
  const [conversationHistory, setConversationHistory] = useState<
    ConversationHistorySummary[]
  >([]);
  const [conversationHistoryStatus, setConversationHistoryStatus] = useState<
    'loading' | 'ready' | 'error'
  >('loading');
  const [activeConversationTitle, setActiveConversationTitle] = useState<
    string | null
  >(null);
  const [renameDraft, setRenameDraft] = useState<{
    id: string;
    title: string;
  } | null>(null);
  const [renameStatus, setRenameStatus] = useState<'idle' | 'saving' | 'error'>(
    'idle'
  );
  const [renameError, setRenameError] = useState<string | null>(null);
  const [deleteDraft, setDeleteDraft] =
    useState<ConversationHistorySummary | null>(null);
  const [deleteStatus, setDeleteStatus] = useState<
    'idle' | 'deleting' | 'error'
  >('idle');
  const [deleteError, setDeleteError] = useState<string | null>(null);

  // Build available tools list - db-query only visible to admins
  const availableTools = useMemo<Tool[]>(() => {
    const tools: Tool[] = [
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
    ];

    // Only show Database tool to authenticated admins
    if (isAdmin) {
      tools.push({
        id: CONFIG_TOOL_ID,
        name: t('chat.tools.configName', 'Config'),
        description: t(
          'chat.tools.config',
          'Read and update admin configuration'
        ),
        icon: <Settings2 className="h-3.5 w-3.5" aria-hidden="true" />,
      });
      tools.push({
        id: 'db-query',
        name: t('chat.tools.databaseName'),
        description: t('chat.tools.database'),
        icon: <Database className="h-3.5 w-3.5" aria-hidden="true" />,
      });
    }

    return tools;
  }, [isAdmin, t]);

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

  const handleShareSecretsToggle = useCallback(
    async (checked: boolean) => {
      setShareSecrets(checked);
      if (!checked) {
        setSecretsForRedaction([]);
        return;
      }
      try {
        const metadata = await refreshAdminConfigRedactionMetadata({
          shareSecrets: true,
          fetchJson,
        });
        setSecretsForRedaction(metadata.secretValues);
      } catch {
        setSecretsForRedaction([]);
      }
    },
    [fetchJson]
  );

  // Reachout settings (public)
  useEffect(() => {
    let isCancelled = false;

    async function fetchReachout() {
      try {
        const res = await fetch(`${API_BASE}/settings/public`);
        if (!res.ok) return;
        const data = await res.json();
        const s = (data?.settings ?? {}) as Record<string, string>;

        if (isCancelled) return;

        setReachoutEnabled(
          String(s.reachout_enabled ?? 'false').toLowerCase() === 'true'
        );
        const mode = String(s.reachout_mode ?? 'support').toLowerCase();
        if (mode === 'feedback' || mode === 'help' || mode === 'support') {
          setReachoutMode(mode);
        } else {
          setReachoutMode('support');
        }

        setReachoutOverrides({
          title:
            typeof s.reachout_title === 'string' ? s.reachout_title : undefined,
          description:
            typeof s.reachout_description === 'string'
              ? s.reachout_description
              : undefined,
          buttonLabel:
            typeof s.reachout_button_label === 'string'
              ? s.reachout_button_label
              : undefined,
          successMessage:
            typeof s.reachout_success_message === 'string'
              ? s.reachout_success_message
              : undefined,
        });
      } catch {
        // Best-effort: feature remains hidden if fetch fails.
      }
    }

    fetchReachout();

    return () => {
      isCancelled = true;
    };
  }, []);

  const loadConversationHistory = useCallback(async () => {
    setConversationHistoryStatus('loading');
    try {
      const res = await fetch(`${API_BASE}/query/sessions`, {
        credentials: 'include',
      });
      if (!res.ok) {
        setConversationHistoryStatus('error');
        return;
      }

      const payload = await res.json();
      const rawConversations: unknown[] = Array.isArray(payload?.conversations)
        ? payload.conversations
        : [];
      setConversationHistory(
        rawConversations
          .map(conversationHistorySummaryFromApi)
          .filter((conversation): conversation is ConversationHistorySummary =>
            Boolean(conversation)
          )
      );
      setConversationHistoryStatus('ready');
    } catch {
      setConversationHistoryStatus('error');
    }
  }, []);

  const handleUserTurnCompleted = useCallback(() => {
    void loadConversationHistory();
  }, [loadConversationHistory]);

  const handleUserAuthFailure = useCallback(
    (status: 401 | 403) => {
      if (status === 403) {
        localStorage.setItem(STORAGE_KEYS.USER_APPROVED, 'false');
        navigate('/pending');
        return;
      }
      navigate('/login');
    },
    [navigate]
  );

  useEffect(() => {
    let cancelled = false;

    async function fetchConversationHistory() {
      await loadConversationHistory();
      if (cancelled) return;
    }

    fetchConversationHistory();

    return () => {
      cancelled = true;
    };
  }, [loadConversationHistory]);

  // Check auth, onboarding, and approval status on mount
  useEffect(() => {
    if (adminSessionChecking) return;

    let isCancelled = false;
    const userEmail = localStorage.getItem(STORAGE_KEYS.USER_EMAIL);

    // Not authenticated at all - redirect to login
    if (!isAdmin && !userEmail) {
      navigate('/login');
      return;
    }

    // Keep onboarding enforcement server-authoritative for returning users.
    // User Approval gates Conversation access after required onboarding steps.
    if (!isAdmin) {
      const checkOnboardingStatus = async () => {
        const approved = localStorage.getItem(STORAGE_KEYS.USER_APPROVED);

        try {
          const response = await fetch(
            `${API_BASE}/users/me/onboarding-status`,
            {
              credentials: 'include',
            }
          );

          if (isCancelled) return;

          if (response.status === 401) {
            navigate('/login');
            return;
          }

          if (!response.ok) {
            if (approved === 'false') {
              navigate('/pending');
            }
            return;
          }

          const status = await response.json();

          if (isCancelled) return;

          const effectiveTypeId = status.effective_user_type_id ?? null;
          saveSelectedUserTypeId(effectiveTypeId);

          if (status.needs_user_type) {
            navigate('/user-type');
            return;
          }

          if (status.needs_onboarding) {
            navigate('/profile');
            return;
          }

          if (approved === 'false') {
            navigate('/pending');
          }
        } catch (err) {
          console.error('Failed to fetch onboarding status:', err);
          if (!isCancelled) {
            const approved = localStorage.getItem(STORAGE_KEYS.USER_APPROVED);
            if (approved === 'false') {
              navigate('/pending');
            }
          }
        }
      };

      checkOnboardingStatus();
    }

    return () => {
      isCancelled = true;
    };
  }, [adminSessionChecking, isAdmin, navigate]);

  // Fetch session defaults from admin config
  useEffect(() => {
    if (sessionDefaultsLoaded || adminSessionChecking) return;

    const fetchSessionDefaults = async () => {
      try {
        const userTypeId = getSelectedUserTypeId();
        const url =
          userTypeId !== null
            ? `${API_BASE}/session-defaults?user_type_id=${userTypeId}`
            : `${API_BASE}/session-defaults`;
        const res = await fetch(url);
        if (res.ok) {
          const data = await res.json();
          // Admin conversations show Tool Set controls, but start opt-in.
          if (isAdmin) {
            dispatchConversation({
              type: 'selectedToolsChanged',
              selectedTools: [],
            });
          } else {
            const defaults = resolveUserConversationSessionDefaults(data);
            setUserSelectedTools(defaults.tools);
            if (defaults.documentIds.length > 0) {
              setPendingDefaultDocs(defaults.documentIds);
            }
          }
        } else {
          // Non-2xx response - fail closed for non-admin user Tool Sets.
          console.warn('Failed to fetch session defaults:', res.status);
          if (isAdmin) {
            dispatchConversation({
              type: 'selectedToolsChanged',
              selectedTools: [],
            });
          } else {
            setUserSelectedTools([]);
          }
        }
      } catch (err) {
        console.error('Failed to fetch session defaults:', err);
        // Fail closed for non-admin user Tool Sets on defaults load errors.
        if (isAdmin) {
          dispatchConversation({
            type: 'selectedToolsChanged',
            selectedTools: [],
          });
        } else {
          setUserSelectedTools([]);
        }
      } finally {
        setSessionDefaultsLoaded(true);
      }
    };

    fetchSessionDefaults();
  }, [adminSessionChecking, isAdmin, sessionDefaultsLoaded]);

  // Fetch available documents from ingest jobs
  useEffect(() => {
    const fetchDocuments = async () => {
      try {
        const res = await fetch(`${API_BASE}/ingest/jobs`, {
          credentials: 'include',
        });
        if (res.ok) {
          const data = await res.json();
          const docs: DocumentSource[] = (data.jobs || [])
            .filter(
              (job: { status: string }) =>
                job.status === 'completed' ||
                job.status === 'completed_with_errors'
            )
            .map(
              (job: {
                job_id: string;
                filename: string;
                total_chunks: number;
              }) => ({
                id: job.job_id,
                name: job.filename.replace(/\.(pdf|txt|md)$/i, ''),
                description: `${job.total_chunks} chunks`,
                tags: [job.filename.split('.').pop()?.toUpperCase() || 'DOC'],
              })
            );
          setDocuments(docs);
        }
      } catch (e) {
        console.error(t('errors.failedToFetchDocuments'), e);
      }
    };
    fetchDocuments();
  }, []);

  // Apply pending default documents once documents are loaded
  useEffect(() => {
    if (pendingDefaultDocs.length > 0 && documents.length > 0) {
      // Filter to only include IDs that exist in the documents list
      const validIds = pendingDefaultDocs.filter((id) =>
        documents.some((d) => d.id === id)
      );
      if (validIds.length > 0) {
        if (isAdmin) {
          dispatchConversation({
            type: 'selectedDocumentsChanged',
            selectedDocuments: validIds,
          });
        } else {
          setUserSelectedDocuments(validIds);
        }
      }
      setPendingDefaultDocs([]);
    }
  }, [documents, isAdmin, pendingDefaultDocs]);

  const handleToolToggle = useCallback(
    (toolId: string) => {
      if (!isAdmin && ADMIN_ONLY_TOOL_IDS.has(toolId)) return;

      if (toolId === CONFIG_TOOL_ID && selectedTools.includes(toolId)) {
        setShareSecrets(false);
        setSecretsForRedaction([]);
      }
      if (isAdmin) {
        dispatchConversation({ type: 'toolToggled', toolId });
      } else {
        setUserSelectedTools((tools) =>
          tools.includes(toolId)
            ? tools.filter((tool) => tool !== toolId)
            : [...tools, toolId]
        );
      }
    },
    [isAdmin, selectedTools]
  );

  const handleDocumentToggle = useCallback(
    (docId: string) => {
      const selectedAfterToggle = !selectedDocuments.includes(docId);
      if (isAdmin) {
        dispatchConversation({ type: 'documentToggled', documentId: docId });
      } else {
        setUserSelectedDocuments((documentIds) =>
          documentIds.includes(docId)
            ? documentIds.filter((documentId) => documentId !== docId)
            : [...documentIds, docId]
        );
      }
      if (
        isAdmin &&
        selectedAfterToggle &&
        !selectedTools.includes(KNOWLEDGE_TOOL_ID)
      ) {
        dispatchConversation({
          type: 'toolToggled',
          toolId: KNOWLEDGE_TOOL_ID,
        });
      }
    },
    [isAdmin, selectedDocuments, selectedTools]
  );

  const reportConversationError = useCallback(
    (message: string) => {
      if (isAdmin) {
        dispatchConversation({ type: 'requestFailed', message });
      } else {
        userConversationRef.current?.fail(message);
      }
    },
    [isAdmin]
  );

  const handleConversationSelect = useCallback(
    async (conversation: ConversationHistorySummary) => {
      activeTurnRef.current?.abort();
      try {
        const res = await fetch(
          `${API_BASE}/query/session/${encodeURIComponent(conversation.id)}`,
          {
            credentials: 'include',
          }
        );
        if (!res.ok) {
          reportConversationError(await readErrorDetail(res));
          return;
        }
        const payload = await res.json();
        const view = conversationSessionViewFromApi(payload);
        if (!view) {
          reportConversationError(
            t(
              'chat.sessions.resumeInvalid',
              'Unable to load that Conversation.'
            )
          );
          return;
        }
        if (isAdmin) {
          dispatchConversation({
            type: 'conversationHydrated',
            sessionId: view.id,
            turns: view.turns,
          });
        } else {
          userConversationRef.current?.hydrate(view.id, view.turns);
        }
        setActiveConversationTitle(view.title ?? conversation.title);
        setRenameDraft(null);
        setRenameStatus('idle');
        setRenameError(null);
        setDeleteDraft(null);
        setDeleteStatus('idle');
        setDeleteError(null);
      } catch (err) {
        reportConversationError(
          err instanceof Error
            ? err.message
            : t(
                'chat.sessions.resumeInvalid',
                'Unable to load that Conversation.'
              )
        );
      }
    },
    [isAdmin, reportConversationError, t]
  );

  const handleRenameSubmit = useCallback(
    async (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      if (!renameDraft) return;
      const title = renameDraft.title.trim();
      if (!title) {
        setRenameStatus('error');
        setRenameError(
          t('chat.sessions.renameEmpty', 'Conversation title is required.')
        );
        return;
      }

      setRenameStatus('saving');
      setRenameError(null);
      try {
        const res = await fetch(
          `${API_BASE}/query/session/${encodeURIComponent(renameDraft.id)}`,
          {
            method: 'PATCH',
            headers: {
              'Content-Type': 'application/json',
            },
            credentials: 'include',
            body: JSON.stringify({ title }),
          }
        );
        if (!res.ok) {
          throw new Error(await readErrorDetail(res));
        }
        const payload = await res.json();
        const confirmedTitle =
          typeof payload?.title === 'string' && payload.title.trim()
            ? payload.title.trim()
            : title;

        setConversationHistory((existing) =>
          existing.map((conversation) =>
            conversation.id === renameDraft.id
              ? { ...conversation, title: confirmedTitle }
              : conversation
          )
        );
        if (conversationSessionId === renameDraft.id) {
          setActiveConversationTitle(confirmedTitle);
        }
        setRenameDraft(null);
        setRenameStatus('idle');
      } catch (err) {
        setRenameStatus('error');
        setRenameError(
          err instanceof Error
            ? err.message
            : t('chat.sessions.renameError', 'Unable to rename Conversation.')
        );
      }
    },
    [conversationSessionId, renameDraft, t]
  );

  const handleDeleteConversation = useCallback(async () => {
    if (!deleteDraft) return;

    if (conversationSessionId === deleteDraft.id)
      activeTurnRef.current?.abort();
    setDeleteStatus('deleting');
    setDeleteError(null);
    try {
      const res = await fetch(
        `${API_BASE}/query/session/${encodeURIComponent(deleteDraft.id)}`,
        {
          method: 'DELETE',
          credentials: 'include',
        }
      );
      if (!res.ok) {
        throw new Error(await readErrorDetail(res));
      }

      setConversationHistory((existing) =>
        existing.filter((conversation) => conversation.id !== deleteDraft.id)
      );
      if (conversationSessionId === deleteDraft.id) {
        if (isAdmin) {
          resetAdminConversation();
        } else {
          userConversationRef.current?.reset();
        }
        setActiveConversationTitle(null);
      }
      setDeleteDraft(null);
      setDeleteStatus('idle');
      setDeleteError(null);
    } catch (err) {
      setDeleteStatus('error');
      setDeleteError(
        err instanceof Error
          ? err.message
          : t('chat.sessions.deleteError', 'Unable to delete Conversation.')
      );
    }
  }, [conversationSessionId, deleteDraft, isAdmin, resetAdminConversation, t]);

  const generateMessageId = () =>
    `msg-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;

  const dispatchStreamEvent = (
    event: string,
    payload: Record<string, unknown>,
    assistantTurnId?: string | null
  ) => {
    const action = adaptSageStreamEvent(event, payload, assistantTurnId);
    if (action) dispatchConversation(action);
    return action;
  };

  const handleSend = async (content: string) => {
    activeTurnRef.current?.abort();
    const turnController = new AbortController();
    activeTurnRef.current = turnController;
    const hasConfigTool = configToolEnabled;

    dispatchConversation({
      type: 'userTurnSubmitted',
      id: generateMessageId(),
      content,
    });

    try {
      const backendTools = actorScopedSelectedTools;
      let secretsForThisRequest = secretsForRedaction;
      if (hasConfigTool && shareSecrets) {
        const metadata = await refreshAdminConfigRedactionMetadata({
          shareSecrets: true,
          fetchJson,
        });
        secretsForThisRequest = metadata.secretValues;
        setSecretsForRedaction(metadata.secretValues);
      }

      let response: Response;
      {
        let streamed = false;
        let streamMessageId: string | null = null;
        let streamContent = '';
        let streamSessionId: string | null = null;
        let streamReportedError = false;
        const notifiedAffectedAreas = new Set<string>();
        const notifyAffectedAreas = (payload: unknown) => {
          const freshAreas = readAdminConfigAffectedAreas(payload).filter(
            (area) => !notifiedAffectedAreas.has(area)
          );
          freshAreas.forEach((area) => notifiedAffectedAreas.add(area));
          notifyAdminConfigChanged(freshAreas);
        };
        try {
          await sendLlmChatStreamWithUnifiedTools({
            signal: turnController.signal,
            content,
            tools: backendTools,
            sessionId: conversationSessionId,
            jobIds: selectedDocuments,
            adminUserLookup: isAdmin && backendTools.includes('db-query'),
            onEvent: (event, payload) => {
              if (turnController.signal.aborted) return;
              const data = payload as Record<string, unknown>;
              notifyAffectedAreas(data);
              if (event === 'assistant_message_started') {
                if (typeof data.message_id !== 'string')
                  data.message_id = generateMessageId();
                const id = data.message_id as string;
                streamMessageId = id;
                streamSessionId =
                  typeof data.session_id === 'string'
                    ? data.session_id
                    : streamSessionId;
                dispatchConversation({
                  type: 'assistantTurnStarted',
                  id,
                  sessionId: streamSessionId,
                  traceStatus: t(
                    'chat.trace.finalizing',
                    'Finalizing response...'
                  ),
                });
              } else if (event === 'answer_delta' && streamMessageId) {
                const delta = typeof data.delta === 'string' ? data.delta : '';
                streamContent += delta;
                if (!shareSecrets) {
                  dispatchConversation({
                    type: 'assistantContentReplaced',
                    assistantTurnId: streamMessageId,
                    content: streamContent,
                  });
                }
              } else if (event === 'done') {
                if (typeof data.session_id === 'string')
                  streamSessionId = data.session_id;
                if (streamMessageId && shareSecrets) {
                  dispatchConversation({
                    type: 'assistantContentReplaced',
                    assistantTurnId: streamMessageId,
                    content: redactSecrets(
                      streamContent,
                      secretsForThisRequest
                    ),
                  });
                }
                dispatchStreamEvent(event, data, streamMessageId);
              } else if (event === 'error') {
                streamReportedError = true;
                throw new Error(
                  formatProviderStreamError(
                    typeof data.detail === 'string' ? data.detail : data,
                    t('errors.failedToSendMessage'),
                    { recordAdminInstrumentation: hasConfigTool }
                  )
                );
              } else if (streamMessageId) {
                dispatchStreamEvent(event, data, streamMessageId);
              }
            },
          });
          if (streamSessionId)
            dispatchConversation({
              type: 'conversationSessionChanged',
              sessionId: streamSessionId,
            });
          if (streamMessageId)
            dispatchConversation({
              type: 'assistantTurnFinished',
              sessionId: streamSessionId,
            });
          streamed = true;
        } catch (streamError) {
          if (activeTurnRef.current !== turnController) return;
          if (streamMessageId && streamContent) {
            dispatchConversation({
              type: 'assistantContentReplaced',
              assistantTurnId: streamMessageId,
              content: shareSecrets
                ? redactSecrets(streamContent, secretsForThisRequest)
                : streamContent,
            });
          }
          if (turnController.signal.aborted) return;
          const errorMessage =
            streamError instanceof Error
              ? streamError.message
              : t('errors.failedToSendMessage');

          if (streamMessageId && streamContent.trim()) {
            dispatchConversation({
              type: 'assistantTurnFailed',
              assistantTurnId: streamMessageId,
              message: errorMessage,
            });
            return;
          }

          if (streamReportedError) {
            if (streamMessageId) {
              dispatchConversation({
                type: 'assistantTurnFailed',
                assistantTurnId: streamMessageId,
                message: errorMessage,
              });
            } else {
              dispatchConversation({
                type: 'requestFailed',
                message: errorMessage,
              });
            }
            return;
          }

          if (streamMessageId) {
            dispatchConversation({
              type: 'assistantTurnFailed',
              assistantTurnId: streamMessageId,
              message: errorMessage,
            });
            dispatchConversation({ type: 'requestErrorDismissed' });
          }
          if (
            streamError &&
            typeof streamError === 'object' &&
            'streamStarted' in streamError &&
            streamError.streamStarted
          ) {
            throw streamError;
          }
          if (hasConfigTool) {
            throw streamError;
          }
          console.warn(
            'Streaming chat failed; falling back to non-streaming chat:',
            streamError
          );
        }
        if (streamed) {
          await loadConversationHistory();
          return;
        }

        response = await sendLlmChatWithUnifiedTools({
          signal: turnController.signal,
          content,
          tools: backendTools,
          sessionId: conversationSessionId,
          jobIds: selectedDocuments,
          adminUserLookup: isAdmin && backendTools.includes('db-query'),
        });
      }

      // Handle auth errors
      if (response.status === 401) {
        // Token invalid/expired
        navigate(isAdmin ? '/admin' : '/login');
        return;
      }
      if (response.status === 403) {
        // Not approved - update localStorage and redirect
        localStorage.setItem(STORAGE_KEYS.USER_APPROVED, 'false');
        navigate('/pending');
        return;
      }

      if (!response.ok) {
        const detail = await readErrorDetail(response);
        throw new Error(
          formatProviderResponseError(detail, `HTTP ${response.status}`, {
            recordAdminInstrumentation: hasConfigTool,
          })
        );
      }

      const data = await response.json();
      notifyAdminConfigChanged(readAdminConfigAffectedAreas(data));

      const responseContent = data.message;
      if (data.session_id) {
        dispatchConversation({
          type: 'conversationSessionChanged',
          sessionId: data.session_id,
        });
      }

      dispatchConversation({
        type: 'assistantTurnCompleted',
        id:
          typeof data.message_id === 'string'
            ? data.message_id
            : generateMessageId(),
        content: shareSecrets
          ? redactSecrets(responseContent, secretsForThisRequest)
          : responseContent,
        trace: data.trace ?? null,
        sessionId:
          typeof data.session_id === 'string' ? data.session_id : undefined,
      });

      await loadConversationHistory();
    } catch (e) {
      if (turnController.signal.aborted) return;
      dispatchConversation({
        type: 'requestFailed',
        message:
          e instanceof Error ? e.message : t('errors.failedToSendMessage'),
      });
    } finally {
      turnController.abort();
      if (activeTurnRef.current === turnController) {
        activeTurnRef.current = null;
        dispatchConversation({ type: 'assistantTurnFinished' });
      }
    }
  };

  const handleNewChat = () => {
    activeTurnRef.current?.abort();
    if (isAdmin) {
      resetAdminConversation();
    } else {
      userConversationRef.current?.reset();
    }
    setShareSecrets(false);
    setSecretsForRedaction([]);
    setActiveConversationTitle(null);
    setRenameDraft(null);
    setRenameStatus('idle');
    setRenameError(null);
    setDeleteDraft(null);
    setDeleteStatus('idle');
    setDeleteError(null);
  };

  const rightActions = (
    <>
      <LanguageSwitcher />
      {reachoutEnabled && (
        <IconButton
          label={t(
            `reachout.mode.${reachoutMode}.openButton`,
            reachoutMode === 'feedback'
              ? 'Send feedback'
              : reachoutMode === 'help'
                ? 'Get help'
                : 'Contact support'
          )}
          onClick={() => setReachoutOpen(true)}
          title={t(
            `reachout.mode.${reachoutMode}.openButton`,
            reachoutMode === 'feedback'
              ? 'Send feedback'
              : reachoutMode === 'help'
                ? 'Get help'
                : 'Contact support'
          )}
        >
          <Mail className="h-4 w-4" aria-hidden="true" />
        </IconButton>
      )}
      <IconButton
        label={t('chat.messages.newConversation')}
        onClick={handleNewChat}
        title={t('chat.messages.newConversation')}
      >
        <Plus className="h-4 w-4" aria-hidden="true" />
      </IconButton>
    </>
  );

  const header = <AppHeader rightActions={rightActions} />;
  const activeHistoryConversation = conversationHistory.find(
    (conversation) => conversation.id === conversationSessionId
  );
  const sessionTitle =
    activeHistoryConversation?.title ||
    activeConversationTitle ||
    messages.find((message) => message.role === 'user')?.content.trim() ||
    t('chat.sessions.current', 'Current chat');
  const sessionMeta =
    messages.length === 0
      ? t('chat.sessions.empty', 'Empty')
      : t('chat.sessions.messageCount', {
          count: messages.length,
          defaultValue:
            messages.length === 1 ? '1 message' : `${messages.length} messages`,
        });
  const sessionSidebar = (
    <nav
      aria-label={t('chat.sessions.ariaLabel', 'Chat sessions')}
      className="flex h-full flex-col gap-3 p-3"
    >
      <Button
        onClick={handleNewChat}
        variant="primary"
        size="sm"
        leadingIcon={<Plus className="h-4 w-4" aria-hidden="true" />}
        className="w-full justify-start"
        data-dismiss-sidebar="true"
      >
        {t('chat.new', 'New chat')}
      </Button>
      <div className="space-y-1">
        <div className="px-2 text-[11px] font-medium uppercase tracking-[0.08em] text-text-muted">
          {t('chat.listTitle', 'Chats')}
        </div>
        {conversationHistoryStatus === 'loading' && (
          <div className="px-2 py-1 text-xs text-text-muted">
            {t('chat.sessions.loading', 'Loading chats...')}
          </div>
        )}
        {conversationHistoryStatus === 'error' && (
          <div role="note" className="px-2 py-1 text-xs text-text-muted">
            {t('chat.sessions.error', 'Unable to load chats')}
          </div>
        )}
        {conversationHistory.map((conversation) => {
          const meta =
            conversation.messageCount === null
              ? t('chat.sessions.saved', 'Saved chat')
              : t('chat.sessions.messageCount', {
                  count: conversation.messageCount,
                  defaultValue:
                    conversation.messageCount === 1
                      ? '1 message'
                      : `${conversation.messageCount} messages`,
                });
          if (deleteDraft?.id === conversation.id) {
            return (
              <div
                key={conversation.id}
                className="rounded-lg border border-danger/40 bg-surface p-2"
              >
                <div className="text-sm font-medium text-text">
                  {t('chat.sessions.deleteConfirmTitle', {
                    title: conversation.title,
                    defaultValue: `Delete "${conversation.title}"?`,
                  })}
                </div>
                <div className="mt-1 text-xs text-text-muted">
                  {t(
                    'chat.sessions.deleteConfirmBody',
                    'This removes the saved Conversation and its Session Memory.'
                  )}
                </div>
                <div className="mt-2 flex items-center justify-end gap-1">
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => {
                      setDeleteDraft(null);
                      setDeleteStatus('idle');
                      setDeleteError(null);
                    }}
                    disabled={deleteStatus === 'deleting'}
                    aria-label={t(
                      'chat.sessions.deleteCancel',
                      'Cancel conversation delete'
                    )}
                  >
                    {t('chat.sessions.cancel', 'Cancel')}
                  </Button>
                  <Button
                    variant="danger"
                    size="sm"
                    onClick={handleDeleteConversation}
                    disabled={deleteStatus === 'deleting'}
                  >
                    {t('chat.sessions.deleteConfirm', 'Delete conversation')}
                  </Button>
                </div>
                {deleteStatus === 'deleting' && (
                  <div role="status" className="mt-1 text-xs text-text-muted">
                    {t('chat.sessions.deleteDeleting', 'Deleting...')}
                  </div>
                )}
                {deleteStatus === 'error' && deleteError && (
                  <div role="note" className="mt-1 text-xs text-danger">
                    {deleteError}
                  </div>
                )}
              </div>
            );
          }
          if (renameDraft?.id === conversation.id) {
            return (
              <form
                key={conversation.id}
                onSubmit={handleRenameSubmit}
                className="rounded-lg border border-border bg-surface p-2"
              >
                <label
                  htmlFor={`conversation-title-${conversation.id}`}
                  className="sr-only"
                >
                  {t('chat.sessions.renameInput', 'Conversation title')}
                </label>
                <input
                  id={`conversation-title-${conversation.id}`}
                  value={renameDraft.title}
                  onChange={(event) =>
                    setRenameDraft({
                      id: conversation.id,
                      title: event.target.value,
                    })
                  }
                  className="w-full rounded-md border border-border bg-surface-raised px-2 py-1.5 text-sm text-text focus-ring"
                  aria-invalid={renameStatus === 'error' ? true : undefined}
                  autoFocus
                />
                <div className="mt-2 flex items-center justify-end gap-1">
                  <IconButton
                    label={t(
                      'chat.sessions.renameCancel',
                      'Cancel conversation rename'
                    )}
                    title={t(
                      'chat.sessions.renameCancel',
                      'Cancel conversation rename'
                    )}
                    onClick={() => {
                      setRenameDraft(null);
                      setRenameStatus('idle');
                      setRenameError(null);
                    }}
                    disabled={renameStatus === 'saving'}
                  >
                    <X className="h-4 w-4" aria-hidden="true" />
                  </IconButton>
                  <IconButton
                    label={t(
                      'chat.sessions.renameSave',
                      'Save conversation title'
                    )}
                    title={t(
                      'chat.sessions.renameSave',
                      'Save conversation title'
                    )}
                    type="submit"
                    disabled={renameStatus === 'saving'}
                  >
                    <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
                  </IconButton>
                </div>
                {renameStatus === 'saving' && (
                  <div role="status" className="mt-1 text-xs text-text-muted">
                    {t('chat.sessions.renameSaving', 'Renaming...')}
                  </div>
                )}
                {renameStatus === 'error' && renameError && (
                  <div role="note" className="mt-1 text-xs text-danger">
                    {renameError}
                  </div>
                )}
              </form>
            );
          }
          return (
            <div
              key={conversation.id}
              className={
                conversation.id === conversationSessionId
                  ? 'flex w-full items-center gap-1 rounded-lg border border-border bg-surface px-2 py-1.5 text-sm text-text shadow-sm'
                  : 'flex w-full items-center gap-1 rounded-lg px-2 py-1.5 text-sm text-text transition-colors hover:bg-surface-overlay'
              }
            >
              <button
                type="button"
                onClick={() => handleConversationSelect(conversation)}
                aria-current={
                  conversation.id === conversationSessionId ? 'page' : undefined
                }
                aria-label={`${conversation.title} ${meta}`}
                className="flex min-w-0 flex-1 items-center gap-2 rounded-md px-0.5 py-0.5 text-left"
                data-dismiss-sidebar="true"
              >
                <MessageSquare
                  className="h-4 w-4 shrink-0 text-text-muted"
                  aria-hidden="true"
                />
                <span className="min-w-0 flex-1">
                  <span className="block truncate">{conversation.title}</span>
                  <span className="block truncate text-xs text-text-muted">
                    {meta}
                  </span>
                </span>
              </button>
              <IconButton
                label={t('chat.sessions.rename', {
                  title: conversation.title,
                  defaultValue: `Rename ${conversation.title}`,
                })}
                title={t('chat.sessions.rename', {
                  title: conversation.title,
                  defaultValue: `Rename ${conversation.title}`,
                })}
                onClick={() => {
                  setRenameDraft({
                    id: conversation.id,
                    title: conversation.title,
                  });
                  setRenameStatus('idle');
                  setRenameError(null);
                  setDeleteDraft(null);
                  setDeleteStatus('idle');
                  setDeleteError(null);
                }}
                className="h-8 w-8 p-1.5"
              >
                <Pencil className="h-3.5 w-3.5" aria-hidden="true" />
              </IconButton>
              <IconButton
                label={t('chat.sessions.delete', {
                  title: conversation.title,
                  defaultValue: `Delete ${conversation.title}`,
                })}
                title={t('chat.sessions.delete', {
                  title: conversation.title,
                  defaultValue: `Delete ${conversation.title}`,
                })}
                onClick={() => {
                  setDeleteDraft(conversation);
                  setDeleteStatus('idle');
                  setDeleteError(null);
                  setRenameDraft(null);
                  setRenameStatus('idle');
                  setRenameError(null);
                }}
                variant="danger"
                className="h-8 w-8 p-1.5"
              >
                <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
              </IconButton>
            </div>
          );
        })}
        {!activeHistoryConversation && (
          <button
            type="button"
            aria-current="page"
            aria-label={`${sessionTitle} ${sessionMeta}`}
            className="flex w-full items-center gap-2 rounded-lg border border-border bg-surface px-2.5 py-2 text-left text-sm text-text shadow-sm"
            data-dismiss-sidebar="true"
          >
            <MessageSquare
              className="h-4 w-4 shrink-0 text-accent"
              aria-hidden="true"
            />
            <span className="min-w-0 flex-1">
              <span className="block truncate">{sessionTitle}</span>
              <span className="block truncate text-xs text-text-muted">
                {sessionMeta}
              </span>
            </span>
          </button>
        )}
      </div>
      <div className="mt-auto border-t border-border pt-3">
        <ExportButton messages={messages} />
      </div>
    </nav>
  );
  const conversationTopbar = (
    <div
      role="region"
      aria-label={t('chat.currentChatAria', 'Current chat')}
      className="mx-auto flex w-full max-w-3xl items-center justify-between gap-3"
    >
      <div className="min-w-0">
        <div className="truncate text-sm font-semibold text-text">
          {sessionTitle}
        </div>
        <div className="truncate text-xs text-text-muted">{sessionMeta}</div>
      </div>
      <Button
        onClick={handleNewChat}
        aria-label={t('chat.newFromCurrent', 'Start a new chat')}
        variant="secondary"
        size="sm"
        leadingIcon={<Plus className="h-4 w-4" aria-hidden="true" />}
        className="shrink-0"
      >
        {t('chat.new', 'New chat')}
      </Button>
    </div>
  );

  const inputToolbar = isAdmin ? (
    <section
      aria-label={t('chat.composerContextAria', 'Composer context')}
      className="flex w-full flex-col gap-2"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[11px] font-medium text-text-muted">
          {t('chat.tools.label', 'Tools')}
        </span>
        <ToolSelector
          tools={availableTools}
          selectedTools={selectedTools}
          onToggle={handleToolToggle}
          compact
        />
        {isAdmin && selectedTools.includes(CONFIG_TOOL_ID) && (
          <>
            <div className="h-4 w-px bg-border" />
            <label
              htmlFor="share-secrets-toggle-compact"
              className="flex items-center gap-2 cursor-pointer"
            >
              <input
                id="share-secrets-toggle-compact"
                type="checkbox"
                checked={shareSecrets}
                onChange={(e) => {
                  void handleShareSecretsToggle(e.target.checked);
                }}
                className="w-3 h-3"
              />
              <EyeOff className="w-3 h-3 text-text-muted" />
              <span className="text-[10px] text-text-muted">
                {t(
                  'admin.configAssistant.shareSecretsTitle',
                  'Share secret env vars'
                )}
              </span>
            </label>
          </>
        )}
        {selectedTools.includes(KNOWLEDGE_TOOL_ID) && (
          <>
            <div className="h-4 w-px bg-border" />
            <span className="text-[11px] font-medium text-text-muted">
              {t('chat.documentsContextTitle', 'Documents')}
            </span>
            <DocumentScope
              selectedDocuments={selectedDocuments}
              onToggle={handleDocumentToggle}
              documents={documents}
              compact
            />
          </>
        )}
      </div>
    </section>
  ) : null;
  const conversationTurns = buildConversationSurfaceTurns(messages);
  const threadNotices = (
    <div className="mt-4 space-y-2">
      {error && (
        <Callout
          label={t('chat.errors.requestLabel', 'Chat request error')}
          tone="error"
          className="flex items-center gap-3 animate-fade-in shadow-sm"
        >
          <AlertCircle
            className="h-4 w-4 shrink-0 text-error"
            aria-hidden="true"
          />
          <span className="flex-1">{error}</span>
          <IconButton
            label={t('common.close', 'Close')}
            onClick={() =>
              dispatchConversation({ type: 'requestErrorDismissed' })
            }
            variant="ghost"
            size="sm"
            className="text-error hover:bg-error/10"
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </IconButton>
        </Callout>
      )}
      {error &&
        isAdmin &&
        selectedTools.includes(CONFIG_TOOL_ID) &&
        shouldOfferNewAssistantConversation(classifyProviderError(error)) && (
          <button
            type="button"
            onClick={handleNewChat}
            className="inline-flex items-center gap-2 px-3 py-2 rounded-xl bg-surface-raised border border-border text-text hover:bg-surface-overlay transition-colors text-sm font-medium"
          >
            {t('admin.configAssistant.startNewConversation')}
          </button>
        )}
    </div>
  );

  return (
    <ChatContainer
      header={header}
      sidebar={sessionSidebar}
      topbar={conversationTopbar}
    >
      <ReachoutModal
        open={reachoutOpen}
        mode={reachoutMode}
        overrides={reachoutOverrides}
        onClose={() => setReachoutOpen(false)}
      />

      {isAdmin ? (
        <ConversationSurface
          activityAudience="admin"
          turns={conversationTurns}
          onSend={handleSend}
          isRunning={isLoading}
          onStop={() => activeTurnRef.current?.abort()}
          placeholder={t('chat.input.placeholder')}
          toolbar={inputToolbar}
          notices={threadNotices}
          hasPersistedSession={Boolean(conversationSessionId)}
          // Authenticated Admins keep the expanded Activity posture on the
          // primary shared surface; Users below get the collapsed default.
          // See #636.
          defaultActivityOpen
        />
      ) : (
        <UserConversation
          ref={userConversationRef}
          selectedTools={userSelectedTools}
          selectedDocuments={userSelectedDocuments}
          placeholder={t('chat.input.placeholder')}
          onSnapshot={setUserConversationState}
          onTerminalTurn={handleUserTurnCompleted}
          onAuthFailure={handleUserAuthFailure}
        />
      )}
    </ChatContainer>
  );
}
