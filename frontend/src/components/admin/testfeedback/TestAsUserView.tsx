import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2, LogOut, Play, RotateCcw, Save, UserCog } from 'lucide-react';
import {
  UserConversation,
  type ConversationUiState,
  type UserConversationHandle,
  type UserConversationTerminalTurn,
} from '../../chat';
import { Button, Callout, Card } from '../../ui';
import {
  createSessionLog,
  getImpersonationStatus,
  listUserTypes,
  provisionTestUser,
  requestImpersonationToken,
  saveTranscript,
  type AdminUserType,
  type TranscriptTurn,
} from '../../../utils/sessionLogsApi';
import { API_BASE } from '../../../types/onboarding';
import { resolveUserConversationSessionDefaults } from '../../../utils/sessionDefaults';

interface UserSessionDefaults {
  tools: string[];
  documentIds: string[];
}

interface ActiveSession {
  testUserId: number;
  userTypeId: number | null;
  personaName: string;
  token: string;
  tools: string[];
  documentIds: string[];
}

type TerminalTurnsById = Record<string, UserConversationTerminalTurn>;

interface CompletedTranscript {
  turns: TranscriptTurn[];
  sageSessionId: string | null;
}

function sessionDefaultsUrl(userTypeId: number | null): string {
  if (userTypeId === null) return `${API_BASE}/session-defaults`;
  return `${API_BASE}/session-defaults?user_type_id=${encodeURIComponent(
    String(userTypeId)
  )}`;
}

async function fetchUserSessionDefaults(
  userTypeId: number | null
): Promise<UserSessionDefaults> {
  try {
    const response = await fetch(sessionDefaultsUrl(userTypeId), {
      credentials: 'include',
    });
    if (response.ok) {
      const defaults = resolveUserConversationSessionDefaults(
        await response.json()
      );
      return {
        tools: defaults.tools,
        documentIds: defaults.documentIds,
      };
    }
  } catch {
    // Match ordinary User Conversation defaults by failing closed.
  }

  return { tools: [], documentIds: [] };
}

function completedTranscript(
  snapshot: ConversationUiState | null,
  terminalTurns: TerminalTurnsById
): CompletedTranscript {
  if (!snapshot) return { turns: [], sageSessionId: null };

  const turns: TranscriptTurn[] = [];
  let sageSessionId: string | null = null;
  const capturedUserTurnIds = new Set<string>();

  for (const assistantTurn of snapshot.turns) {
    if (assistantTurn.role !== 'assistant') continue;
    const terminal = terminalTurns[assistantTurn.id];
    if (!terminal) continue;
    const userTurn = snapshot.turns.find(
      (turn) => turn.id === terminal.userTurnId && turn.role === 'user'
    );
    if (!userTurn || capturedUserTurnIds.has(userTurn.id)) continue;

    capturedUserTurnIds.add(userTurn.id);
    sageSessionId = terminal.sessionId ?? sageSessionId;
    turns.push(
      {
        message_id: userTurn.id,
        role: 'user',
        content: userTurn.content,
        tools_used: [],
      },
      {
        message_id: assistantTurn.id,
        role: 'assistant',
        content: assistantTurn.content,
        trace: assistantTurn.trace,
        tools_used: terminal.toolsUsed,
      }
    );
  }

  return { turns, sageSessionId };
}

/**
 * Admin adapter for a Test User Session. UserConversation owns execution and
 * presentation; this wrapper owns only synthetic identity and encrypted trial
 * capture.
 */
export function TestAsUserView({ onSaved }: { onSaved?: () => void }) {
  const { t } = useTranslation();
  const conversationRef = useRef<UserConversationHandle>(null);
  const pendingLogRef = useRef<{ id: string; title: string } | null>(null);
  const [userTypes, setUserTypes] = useState<AdminUserType[]>([]);
  const [selectedTypeId, setSelectedTypeId] = useState<number | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [session, setSession] = useState<ActiveSession | null>(null);
  const [snapshot, setSnapshot] = useState<ConversationUiState | null>(null);
  const [terminalTurns, setTerminalTurns] = useState<TerminalTurnsById>({});
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [savedNotice, setSavedNotice] = useState<string | null>(null);

  useEffect(() => {
    listUserTypes()
      .then((types) => setUserTypes(types ?? []))
      .catch(() => setUserTypes([]));
  }, []);

  const personaName = (typeId: number | null) =>
    userTypes.find((userType) => userType.id === typeId)?.name ??
    t('adminTestFeedback.test.genericUser', 'User');

  const clearConversationCapture = useCallback(() => {
    pendingLogRef.current = null;
    setSnapshot(null);
    setTerminalTurns({});
    setSaveError(null);
  }, []);

  const startSession = useCallback(async () => {
    setStarting(true);
    setStartError(null);
    setSavedNotice(null);
    try {
      const available = await getImpersonationStatus();
      if (!available) {
        throw new Error(
          t(
            'adminTestFeedback.test.impersonationUnavailable',
            'Test-user impersonation is not available yet'
          )
        );
      }
      const provisioned = await provisionTestUser(selectedTypeId);
      const token = await requestImpersonationToken(provisioned.user_id);
      if (!token?.token) {
        throw new Error(
          t(
            'adminTestFeedback.test.sessionTokenError',
            'Could not create a synthetic user session token'
          )
        );
      }
      const defaults = await fetchUserSessionDefaults(selectedTypeId);
      setSession({
        testUserId: provisioned.user_id,
        userTypeId: selectedTypeId,
        personaName: personaName(selectedTypeId),
        token: token.token,
        tools: defaults.tools,
        documentIds: defaults.documentIds,
      });
      clearConversationCapture();
    } catch (error) {
      setStartError(
        error instanceof Error
          ? error.message
          : t(
              'adminTestFeedback.test.startError',
              'Failed to start a test session'
            )
      );
    } finally {
      setStarting(false);
    }
  }, [clearConversationCapture, selectedTypeId, t, userTypes]);

  const recordTerminalTurn = useCallback(
    (terminalTurn: UserConversationTerminalTurn) => {
      setTerminalTurns((current) => ({
        ...current,
        [terminalTurn.assistantTurnId]: terminalTurn,
      }));
    },
    []
  );

  const confirmDiscardCompletedTurns = useCallback(
    () =>
      completedTranscript(snapshot, terminalTurns).turns.length === 0 ||
      window.confirm(
        t(
          'adminTestFeedback.test.discardUnsavedConfirm',
          'Discard completed turns that have not been saved?'
        )
      ),
    [snapshot, t, terminalTurns]
  );

  const resetConversation = useCallback(() => {
    if (!confirmDiscardCompletedTurns()) return;
    clearConversationCapture();
    conversationRef.current?.reset();
  }, [clearConversationCapture, confirmDiscardCompletedTurns]);

  const exitSession = useCallback(() => {
    if (!confirmDiscardCompletedTurns()) return;

    setSession(null);
    setSavedNotice(null);
    clearConversationCapture();
  }, [clearConversationCapture, confirmDiscardCompletedTurns]);

  const capturedTranscript = completedTranscript(snapshot, terminalTurns);

  const endAndSave = useCallback(async () => {
    const capture = completedTranscript(snapshot, terminalTurns);
    if (!session || capture.turns.length === 0 || snapshot?.isRunning) return;

    setSaving(true);
    setSaveError(null);
    try {
      if (!pendingLogRef.current) {
        const title = `${session.personaName} · ${new Date().toLocaleString()}`;
        const log = await createSessionLog({
          source: 'admin_test',
          title,
          subject_user_id: session.testUserId,
          user_type_id: session.userTypeId,
          sage_session_id: capture.sageSessionId,
        });
        pendingLogRef.current = { id: log.log_id, title };
      }
      const pendingLog = pendingLogRef.current;
      await saveTranscript(pendingLog.id, capture.turns, pendingLog.title);
      setSavedNotice(
        t(
          'adminTestFeedback.test.saved',
          'Trial saved (encrypted). Review it under Feedback.'
        )
      );
      setSession(null);
      clearConversationCapture();
      onSaved?.();
    } catch (error) {
      setSaveError(
        error instanceof Error
          ? error.message
          : t('adminTestFeedback.test.saveError', 'Failed to save trial')
      );
    } finally {
      setSaving(false);
    }
  }, [clearConversationCapture, onSaved, session, snapshot, t, terminalTurns]);

  if (!session) {
    return (
      <Card className="flex flex-col gap-4">
        <div>
          <div className="text-base font-semibold text-text">
            {t('adminTestFeedback.test.pickPersona', 'Pick a persona to test')}
          </div>
          <p className="mt-1 text-sm text-text-secondary">
            {t(
              'adminTestFeedback.test.pickPersonaBody',
              'Provision a test user for one of your user types, then chat as them. The session is saved as an encrypted trial you can rate under Feedback.'
            )}
          </p>
        </div>

        {savedNotice && <Callout tone="success">{savedNotice}</Callout>}
        {startError && <Callout tone="error">{startError}</Callout>}

        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-text-secondary">
              {t('adminTestFeedback.test.userType', 'User type')}
            </span>
            <select
              value={selectedTypeId ?? ''}
              onChange={(event) =>
                setSelectedTypeId(
                  event.target.value ? Number(event.target.value) : null
                )
              }
              className="min-w-56 rounded-lg border border-border bg-surface px-3 py-2 text-sm text-text focus:border-accent focus:outline-none"
            >
              <option value="">
                {t(
                  'adminTestFeedback.test.genericUser',
                  'Generic user (no type)'
                )}
              </option>
              {userTypes.map((userType) => (
                <option key={userType.id} value={userType.id}>
                  {userType.name}
                </option>
              ))}
            </select>
          </label>
          <Button
            variant="primary"
            onClick={() => void startSession()}
            disabled={starting}
            leadingIcon={
              starting ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Play className="h-4 w-4" />
              )
            }
          >
            {t('adminTestFeedback.test.start', 'Start session')}
          </Button>
        </div>
      </Card>
    );
  }

  const conversationRunning = snapshot?.isRunning ?? false;
  const hasConversationTurns = Boolean(snapshot?.turns.length);

  return (
    <div
      role="region"
      aria-label={t(
        'adminTestFeedback.test.workspaceAria',
        'Test User conversation workspace'
      )}
      className="flex h-[clamp(32rem,calc(100dvh_-_13rem),56rem)] min-h-0 flex-col overflow-hidden rounded-xl border border-border bg-surface-raised shadow-sm"
    >
      <UserConversation
        ref={conversationRef}
        selectedTools={session.tools}
        selectedDocuments={session.documentIds}
        authToken={session.token}
        placeholder={t(
          'adminTestFeedback.test.inputPlaceholder',
          'Message the assistant as this user…'
        )}
        onSnapshot={setSnapshot}
        onTerminalTurn={recordTerminalTurn}
        onAuthFailure={() =>
          conversationRef.current?.fail(
            t(
              'adminTestFeedback.test.authFailed',
              'The synthetic User session is no longer authorized.'
            )
          )
        }
        notices={saveError ? <Callout tone="error">{saveError}</Callout> : null}
        toolbar={
          <>
            <div className="mr-auto flex flex-wrap items-center gap-2">
              <div className="inline-flex items-center gap-2 rounded-lg border border-border bg-surface-raised px-2.5 py-1.5 text-sm text-text">
                <UserCog className="h-4 w-4 text-accent" aria-hidden="true" />
                <span className="font-medium">
                  {t(
                    'adminTestFeedback.test.testingAs',
                    'Testing as {{persona}}',
                    { persona: session.personaName }
                  )}
                </span>
              </div>
              <span className="text-xs text-text-muted">
                {t('adminTestFeedback.test.testUserId', 'Test user')} #
                {session.testUserId}
              </span>
            </div>
            <Button
              variant="ghost"
              size="sm"
              onClick={resetConversation}
              disabled={saving || conversationRunning || !hasConversationTurns}
              leadingIcon={<RotateCcw className="h-4 w-4" />}
            >
              {t('common.reset', 'Reset')}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={exitSession}
              disabled={saving || conversationRunning}
              leadingIcon={<LogOut className="h-4 w-4" />}
            >
              {t('common.exit', 'Exit')}
            </Button>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => void endAndSave()}
              disabled={
                saving ||
                conversationRunning ||
                capturedTranscript.turns.length === 0
              }
              leadingIcon={
                saving ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <Save className="h-4 w-4" />
                )
              }
            >
              {t('adminTestFeedback.test.endSave', 'End & save trial')}
            </Button>
          </>
        }
      />
    </div>
  );
}
