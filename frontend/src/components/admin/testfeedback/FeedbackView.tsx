import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Loader2,
  ThumbsUp,
  ThumbsDown,
  Trash2,
  ShieldAlert,
  RefreshCw,
  Download,
  Wrench,
  Info,
} from 'lucide-react';
import { Button, Callout, Card, IconButton } from '../../ui';
import { decryptField, hasNip04Support } from '../../../utils/encryption';
import {
  deleteSessionLog,
  getSessionLog,
  listSessionLogs,
  recordSessionLogPlaintextExport,
  setMessageFeedback,
  type FeedbackRating,
  type SessionLogDetail,
  type SessionLogMetadata,
  type SessionLogTranscriptMessage,
  type TranscriptToolCall,
  type TranscriptTurn,
} from '../../../utils/sessionLogsApi';

interface TurnDraft {
  rating?: FeedbackRating;
  comment: string;
  saving?: boolean;
  saved?: boolean;
}

function downloadBlob(blob: Blob, filename: string) {
  const href = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = href;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(href);
}

interface TranscriptToolSummary {
  id: string;
  name: string;
  summary?: string | null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function isOptionalString(value: unknown): boolean {
  return value == null || typeof value === 'string';
}

function isOptionalStringArray(value: unknown): boolean {
  return (
    value == null ||
    (Array.isArray(value) && value.every((entry) => typeof entry === 'string'))
  );
}

interface ParsedTranscriptToolCall extends Omit<
  TranscriptToolCall,
  'warnings' | 'guarded'
> {
  warnings?: string[] | null;
  guarded?: boolean | null;
}

interface ParsedTranscriptTurn extends Omit<TranscriptTurn, 'tools_used'> {
  tools_used?: ParsedTranscriptToolCall[] | null;
}

/** A decrypted transcript message, identified for feedback. */
interface ReviewTurn extends TranscriptTurn {
  message_id: string;
}

function isTranscriptToolCall(
  value: unknown
): value is ParsedTranscriptToolCall {
  if (!isRecord(value)) return false;
  return (
    typeof value.tool_id === 'string' &&
    typeof value.tool_name === 'string' &&
    isOptionalString(value.query) &&
    isOptionalString(value.output_summary) &&
    isOptionalStringArray(value.warnings) &&
    (value.guarded == null || typeof value.guarded === 'boolean')
  );
}

function isTranscriptTraceTool(value: unknown): boolean {
  if (!isRecord(value)) return false;
  return (
    (typeof value.id === 'string' || typeof value.name === 'string') &&
    isOptionalString(value.id) &&
    isOptionalString(value.name) &&
    isOptionalString(value.output_summary) &&
    isOptionalStringArray(value.warnings)
  );
}

function isTranscriptTurn(value: unknown): value is ParsedTranscriptTurn {
  if (!isRecord(value)) return false;
  const toolsUsed = value.tools_used;
  const trace = value.trace;
  return (
    isOptionalString(value.message_id) &&
    typeof value.role === 'string' &&
    typeof value.content === 'string' &&
    (toolsUsed == null ||
      (Array.isArray(toolsUsed) && toolsUsed.every(isTranscriptToolCall))) &&
    (trace == null ||
      (isRecord(trace) &&
        (trace.tools == null ||
          (Array.isArray(trace.tools) &&
            trace.tools.every(isTranscriptTraceTool)))))
  );
}

/**
 * Decrypted turns paired with the server's plaintext message order. Feedback is
 * saved against each message's ID, so the two must describe the same messages.
 * Logs saved before the message order was recorded have none; their messages
 * are named by position, as the backend names them.
 */
function parseTranscriptTurns(
  plaintext: string,
  messages: SessionLogTranscriptMessage[],
  expectedTurnCount: number,
  integrityError: string
): ReviewTurn[] {
  let parsed: unknown;
  try {
    parsed = JSON.parse(plaintext) as unknown;
  } catch {
    throw new Error(integrityError);
  }
  if (
    typeof parsed !== 'object' ||
    parsed === null ||
    !('turns' in parsed) ||
    !Array.isArray(parsed.turns) ||
    parsed.turns.length !== expectedTurnCount ||
    !parsed.turns.every(isTranscriptTurn)
  ) {
    throw new Error(integrityError);
  }
  const order =
    messages.length > 0
      ? messages
      : parsed.turns.map((turn, index) => ({
          message_id: `legacy-${index}`,
          role: turn.role,
        }));
  if (order.length !== parsed.turns.length) {
    throw new Error(integrityError);
  }
  return parsed.turns.map((turn, index) => {
    const message = order[index];
    if (
      turn.role !== message.role ||
      (turn.message_id != null && turn.message_id !== message.message_id)
    ) {
      throw new Error(integrityError);
    }
    return {
      ...turn,
      message_id: message.message_id,
      tools_used: (turn.tools_used ?? []).map((tool) => ({
        ...tool,
        warnings: tool.warnings ?? [],
        guarded: tool.guarded ?? false,
      })),
    };
  });
}

function transcriptToolSummaries(
  turn: TranscriptTurn
): TranscriptToolSummary[] {
  const seen = new Set<string>();
  const summaries: TranscriptToolSummary[] = [];

  for (const tool of turn.trace?.tools ?? []) {
    const id = tool.id || tool.name;
    if (!id || seen.has(id)) continue;
    seen.add(id);
    summaries.push({
      id,
      name: tool.name || id,
      summary: tool.output_summary,
    });
  }

  for (const tool of turn.tools_used ?? []) {
    const id = tool.tool_id || tool.tool_name;
    if (!id || seen.has(id)) continue;
    seen.add(id);
    summaries.push({
      id,
      name: tool.tool_name || id,
      summary: tool.output_summary,
    });
  }

  return summaries;
}

function TranscriptTraceSummary({ turn }: { turn: TranscriptTurn }) {
  const { t } = useTranslation();
  const tools = transcriptToolSummaries(turn);
  const reasoning = turn.trace?.reasoning?.summary;
  if (tools.length === 0 && !reasoning) return null;

  return (
    <div
      className="mt-1 rounded-lg border border-border bg-surface px-3 py-2 text-xs text-text-secondary"
      aria-label={t('adminFeedback.conversationTrace')}
    >
      {reasoning && <div className="mb-2 leading-relaxed">{reasoning}</div>}
      {tools.length > 0 && (
        <div className="flex flex-col gap-1.5">
          {tools.map((tool) => (
            <div key={tool.id} className="flex items-start gap-2">
              <Wrench
                className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent"
                aria-hidden="true"
              />
              <div className="min-w-0">
                <div className="font-medium text-text">{tool.name}</div>
                {tool.summary && (
                  <div className="mt-0.5 leading-relaxed">{tool.summary}</div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * Feedback view — review a saved trial transcript and rate each turn.
 * Transcripts and comments arrive as NIP-04 ciphertext; we decrypt client-side
 * via the admin's NIP-07 extension. Ratings are plaintext; comments are encrypted.
 */
export function FeedbackView() {
  const { t } = useTranslation();
  const transcriptIntegrityError = t(
    'adminTestFeedback.feedback.transcriptIntegrityError',
    'The transcript is unavailable or incomplete and cannot be exported.'
  );
  const [logs, setLogs] = useState<SessionLogMetadata[]>([]);
  const [loadingList, setLoadingList] = useState(true);
  const [listError, setListError] = useState<string | null>(null);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<SessionLogDetail | null>(null);
  const [turns, setTurns] = useState<ReviewTurn[] | null>(null);
  // Keyed by message ID, so a rating stays on its answer.
  const [drafts, setDrafts] = useState<Record<string, TurnDraft>>({});
  const [undecryptedFeedbackMessages, setUndecryptedFeedbackMessages] =
    useState<string[]>([]);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [feedbackError, setFeedbackError] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const openLogRequestIdRef = useRef(0);
  // Decryption progress, so a pending extension approval is visible instead of
  // looking like a hang. Null when no decrypt is in flight. See #648.
  const [decryptProgress, setDecryptProgress] = useState<{
    done: number;
    total: number;
  } | null>(null);

  const nip04 = useMemo(() => hasNip04Support(), []);

  // Export is gated on a completed client-side decrypt rather than refused
  // after the click. The transcript and comments are NIP-04 ciphertext at rest;
  // the readable copy only exists once every decryption request is approved.
  // Exporting before that produced a file that looked successful but was empty
  // or unreadable (see #493, #643).
  //
  // Server-side enforcement is out of scope by design: the plaintext is
  // assembled in the browser and the backend never holds it.
  const exportBlockedReason:
    | 'no-extension'
    | 'decrypting'
    | 'not-decrypted'
    | 'partial'
    | null = !nip04
    ? 'no-extension'
    : decryptProgress
      ? 'decrypting'
      : !turns
        ? 'not-decrypted'
        : undecryptedFeedbackMessages.length > 0
          ? 'partial'
          : null;

  const exportBlockedMessage = !exportBlockedReason
    ? null
    : exportBlockedReason === 'no-extension'
      ? t(
          'adminTestFeedback.feedback.exportNeedsExtension',
          'Export needs a Nostr extension. Transcripts are encrypted at rest and your extension is what decrypts them.'
        )
      : exportBlockedReason === 'decrypting'
        ? t(
            'adminTestFeedback.feedback.exportDecrypting',
            'Still decrypting. Export becomes available once every request is approved.'
          )
        : exportBlockedReason === 'not-decrypted'
          ? t(
              'adminTestFeedback.feedback.exportNotDecrypted',
              'Open and decrypt the transcript before exporting.'
            )
          : t(
              'adminTestFeedback.feedback.exportFeedbackNotDecrypted',
              'Some feedback comments could not be decrypted. Reopen the transcript and approve every decryption request before exporting.'
            );

  const exportDisabled = exporting || exportBlockedReason !== null;
  const [showExportHelp, setShowExportHelp] = useState(false);

  const loadList = useCallback(async () => {
    setLoadingList(true);
    setListError(null);
    try {
      setLogs(await listSessionLogs());
    } catch (err) {
      setListError(
        err instanceof Error
          ? err.message
          : t(
              'adminTestFeedback.feedback.loadTrialsError',
              'Failed to load trials'
            )
      );
    } finally {
      setLoadingList(false);
    }
  }, [t]);

  useEffect(() => {
    void loadList();
  }, [loadList]);

  const openLog = useCallback(
    async (logId: string) => {
      const requestId = openLogRequestIdRef.current + 1;
      openLogRequestIdRef.current = requestId;
      const isCurrentRequest = () => openLogRequestIdRef.current === requestId;

      setSelectedId(logId);
      setLoadingDetail(true);
      setDetailError(null);
      setFeedbackError(null);
      setExportError(null);
      setDeleteError(null);
      setTurns(null);
      setDrafts({});
      setUndecryptedFeedbackMessages([]);
      setDecryptProgress(null);
      try {
        const full = await getSessionLog(logId);
        if (!isCurrentRequest()) return;
        setDetail(full);

        const hasTranscriptCiphertext = Boolean(full.transcript_ciphertext);
        const hasTranscriptKey = Boolean(full.transcript_ephemeral_pubkey);
        if (
          hasTranscriptCiphertext !== hasTranscriptKey ||
          (!hasTranscriptCiphertext && full.turn_count > 0) ||
          (full.transcript_messages.length > 0 &&
            full.transcript_messages.length !== full.turn_count)
        ) {
          throw new Error(transcriptIntegrityError);
        }

        // Count everything that will need an extension approval up front, so the
        // Admin sees real progress rather than an anonymous spinner. See #648.
        const encryptedCommentCount = full.feedback.filter(
          (fb) => fb.comment_ciphertext
        ).length;
        const transcriptCount =
          full.transcript_ciphertext && full.transcript_ephemeral_pubkey
            ? 1
            : 0;
        const totalDecrypts = transcriptCount + encryptedCommentCount;
        if (totalDecrypts > 0) {
          setDecryptProgress({ done: 0, total: totalDecrypts });
        }

        // Decrypt the transcript ciphertext via NIP-07.
        let parsedTurns: ReviewTurn[] | null = null;
        if (full.transcript_ciphertext && full.transcript_ephemeral_pubkey) {
          const plaintext = await decryptField({
            ciphertext: full.transcript_ciphertext,
            ephemeral_pubkey: full.transcript_ephemeral_pubkey,
          });
          if (!isCurrentRequest()) return;
          if (plaintext == null) {
            throw new Error(
              t(
                'adminTestFeedback.feedback.decryptTranscriptError',
                'Could not decrypt the transcript. Approve the decryption request in your Nostr extension.'
              )
            );
          }
          parsedTurns = parseTranscriptTurns(
            plaintext,
            full.transcript_messages,
            full.turn_count,
            transcriptIntegrityError
          );
          setDecryptProgress((prev) =>
            prev ? { ...prev, done: prev.done + 1 } : prev
          );
        }
        if (!isCurrentRequest()) return;
        setTurns(parsedTurns);

        // Hydrate per-turn drafts from existing (decrypted) feedback.
        //
        // Sequential on purpose. Decrypts are already serialized globally (see
        // encryption.ts), but iterating in order keeps the progress count honest
        // and makes the approval sequence predictable for the Admin. See #648.
        const nextDrafts: Record<string, TurnDraft> = {};
        const nextUndecryptedFeedbackMessages: string[] = [];
        for (const fb of full.feedback) {
          if (!isCurrentRequest()) return;
          let comment = '';
          if (fb.comment_ciphertext) {
            if (!fb.comment_ephemeral_pubkey) {
              nextUndecryptedFeedbackMessages.push(fb.message_id);
            } else {
              try {
                const decryptedComment = await decryptField({
                  ciphertext: fb.comment_ciphertext,
                  ephemeral_pubkey: fb.comment_ephemeral_pubkey,
                });
                if (decryptedComment == null) {
                  nextUndecryptedFeedbackMessages.push(fb.message_id);
                } else {
                  comment = decryptedComment;
                }
              } catch {
                nextUndecryptedFeedbackMessages.push(fb.message_id);
              }
            }
            if (isCurrentRequest()) {
              setDecryptProgress((prev) =>
                prev ? { ...prev, done: prev.done + 1 } : prev
              );
            }
          }
          nextDrafts[fb.message_id] = {
            rating: fb.rating,
            comment,
            saved: true,
          };
        }
        if (!isCurrentRequest()) return;
        setDrafts(nextDrafts);
        setUndecryptedFeedbackMessages(nextUndecryptedFeedbackMessages);
      } catch (err) {
        if (isCurrentRequest()) {
          setDetailError(
            err instanceof Error
              ? err.message
              : t(
                  'adminTestFeedback.feedback.loadTranscriptError',
                  'Failed to load transcript'
                )
          );
        }
      } finally {
        if (isCurrentRequest()) {
          setLoadingDetail(false);
          setDecryptProgress(null);
        }
      }
    },
    [t, transcriptIntegrityError]
  );

  const updateDraft = (messageId: string, patch: Partial<TurnDraft>) => {
    setDrafts((prev) => {
      const existing: TurnDraft = prev[messageId] ?? { comment: '' };
      return {
        ...prev,
        [messageId]: { ...existing, ...patch, saved: false },
      };
    });
  };

  const saveFeedback = useCallback(
    async (messageId: string, rating: FeedbackRating) => {
      if (!selectedId) return;
      const draft = drafts[messageId];
      setFeedbackError(null);
      updateDraft(messageId, { rating, saving: true });
      try {
        await setMessageFeedback(
          selectedId,
          messageId,
          rating,
          draft?.comment?.trim() || null
        );
        setDrafts((prev) => {
          const existing: TurnDraft = prev[messageId] ?? { comment: '' };
          return {
            ...prev,
            [messageId]: {
              ...existing,
              rating,
              saving: false,
              saved: true,
            },
          };
        });
      } catch (err) {
        setFeedbackError(
          err instanceof Error
            ? err.message
            : t(
                'adminTestFeedback.feedback.saveError',
                'Feedback could not be saved'
              )
        );
        // Show the rating that is actually saved, not the one that failed.
        updateDraft(messageId, { rating: draft?.rating, saving: false });
      }
    },
    [selectedId, drafts, t]
  );

  const handleDelete = useCallback(
    async (logId: string) => {
      setDeleteError(null);
      try {
        await deleteSessionLog(logId);
        if (selectedId === logId) {
          setSelectedId(null);
          setDetail(null);
          setTurns(null);
          setUndecryptedFeedbackMessages([]);
        }
        await loadList();
      } catch (err) {
        setDeleteError(
          err instanceof Error
            ? err.message
            : t(
                'adminTestFeedback.feedback.deleteError',
                'Session log could not be deleted'
              )
        );
      }
    },
    [selectedId, loadList, t]
  );

  const handleExport = useCallback(async () => {
    if (!selectedId || !detail) return;
    setExportError(null);
    // Export the already client-side-decrypted transcript + feedback as
    // readable plaintext. The backend only ever holds NIP-04 ciphertext, so the
    // decrypted copy is assembled here (encrypted-at-rest is unchanged). See #493.
    if (!turns) {
      setExportError(
        t(
          'adminTestFeedback.feedback.exportNotDecrypted',
          'Open and decrypt the transcript before exporting.'
        )
      );
      return;
    }
    if (undecryptedFeedbackMessages.length > 0) {
      setExportError(
        t(
          'adminTestFeedback.feedback.exportFeedbackNotDecrypted',
          'Some feedback comments could not be decrypted. Reopen the transcript and approve every decryption request before exporting.'
        )
      );
      return;
    }
    setExporting(true);
    try {
      const positions = new Map(
        turns.map((turn, index) => [turn.message_id, index])
      );
      const feedback = Object.entries(drafts)
        .map(([messageId, draft]) => ({
          message_id: messageId,
          turn_index: positions.get(messageId) ?? null,
          rating: draft.rating ?? null,
          comment: draft.comment || null,
        }))
        .filter((entry) => entry.rating || entry.comment)
        .sort(
          (a, b) =>
            (a.turn_index ?? Number.MAX_SAFE_INTEGER) -
            (b.turn_index ?? Number.MAX_SAFE_INTEGER)
        );

      const exportData = {
        log_id: detail.log_id,
        title: detail.title,
        source: detail.source,
        status: detail.status,
        subject_user_id: detail.subject_user_id,
        user_type_id: detail.user_type_id,
        sage_session_id: detail.sage_session_id,
        turn_count: detail.turn_count,
        created_at: detail.created_at,
        completed_at: detail.completed_at,
        exported_at: new Date().toISOString(),
        note: 'Decrypted export — plaintext transcript and feedback.',
        turns,
        feedback,
      };

      const blob = new Blob([JSON.stringify(exportData, null, 2)], {
        type: 'application/json',
      });
      // Plaintext stays in the browser, but the copied export must still leave
      // audit evidence before the download is allowed to escape active storage.
      await recordSessionLogPlaintextExport(selectedId);
      downloadBlob(blob, `beta-session-log-${selectedId}.json`);
    } catch (err) {
      setExportError(
        err instanceof Error
          ? err.message
          : t('adminTestFeedback.feedback.exportError', 'Session export failed')
      );
    } finally {
      setExporting(false);
    }
  }, [selectedId, detail, turns, drafts, undecryptedFeedbackMessages, t]);

  const sourceLabel = useCallback(
    (source: string) => {
      if (source === 'user') {
        return t('adminTestFeedback.feedback.sourceUser', 'User Conversation');
      }
      if (source === 'admin_test') {
        return t('adminTestFeedback.feedback.sourceTest', 'Test User Session');
      }
      return source;
    },
    [t]
  );

  return (
    <div className="grid gap-4 lg:grid-cols-[18rem_1fr]">
      {/* Trial list */}
      <div className="flex flex-col gap-2">
        <div className="flex items-center justify-between">
          <div className="label">
            {t('adminTestFeedback.feedback.trials', 'Beta Logs')}
          </div>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => void loadList()}
            leadingIcon={<RefreshCw className="h-4 w-4" />}
          >
            {t('common.refresh', 'Refresh')}
          </Button>
        </div>
        {loadingList ? (
          <div className="flex items-center justify-center py-10 text-text-muted">
            <Loader2 className="h-5 w-5 animate-spin" />
          </div>
        ) : listError ? (
          <Callout tone="error">{listError}</Callout>
        ) : logs.length === 0 ? (
          <Card className="py-8 text-center text-sm text-text-secondary">
            {t(
              'adminTestFeedback.feedback.empty',
              'No saved beta logs yet. Run a Test User Session or wait for user conversations.'
            )}
          </Card>
        ) : (
          <ul className="flex flex-col gap-2">
            {logs.map((log) => {
              const active = log.log_id === selectedId;
              return (
                <li key={log.log_id}>
                  <button
                    onClick={() => void openLog(log.log_id)}
                    className={[
                      'w-full rounded-xl border px-3 py-2.5 text-left transition-colors',
                      active
                        ? 'border-accent bg-accent/10'
                        : 'border-border bg-surface-raised hover:bg-surface-overlay',
                    ].join(' ')}
                  >
                    <div className="truncate text-sm font-medium text-text">
                      {log.title ||
                        t(
                          'adminTestFeedback.feedback.untitled',
                          'Untitled conversation'
                        )}
                    </div>
                    <div className="mt-0.5 flex items-center gap-2 text-xs text-text-muted">
                      <span className="rounded bg-surface-overlay px-1.5 py-0.5">
                        {sourceLabel(log.source)}
                      </span>
                      <span>
                        {log.turn_count}{' '}
                        {t('adminTestFeedback.feedback.turns', 'turns')}
                      </span>
                    </div>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      {/* Transcript + per-turn feedback */}
      <div className="min-w-0">
        {!nip04 && (
          <Callout tone="warning" className="mb-4">
            <span className="inline-flex items-center gap-2">
              <ShieldAlert className="h-4 w-4" />
              {t(
                'adminTestFeedback.feedback.noNip04',
                'Connect a Nostr extension (Alby, nos2x) to decrypt transcripts — they are encrypted to your admin key.'
              )}
            </span>
          </Callout>
        )}

        {!selectedId ? (
          <Card className="py-16 text-center text-sm text-text-secondary">
            {t(
              'adminTestFeedback.feedback.selectPrompt',
              'Select a beta log to review its transcript and rate each turn.'
            )}
          </Card>
        ) : loadingDetail ? (
          <div className="flex flex-col items-center gap-4 py-16 text-text-muted">
            <Loader2 className="h-5 w-5 animate-spin" />
            {decryptProgress && (
              // A pending extension approval otherwise looks like a hang: the
              // popup does not always raise itself. See #648.
              <Callout tone="accent">
                <div className="font-medium text-text">
                  {t(
                    'adminTestFeedback.feedback.decryptPendingTitle',
                    'Check your Nostr extension'
                  )}
                </div>
                <p className="mt-1">
                  {t(
                    'adminTestFeedback.feedback.decryptPendingBody',
                    'It is asking permission to decrypt this transcript. It may not open on its own — click the extension icon if you do not see it. Choose the option to remember your choice so it only asks once.'
                  )}
                </p>
                <p className="mt-2 text-xs">
                  {t(
                    'adminTestFeedback.feedback.decryptProgress',
                    'Decrypted {{done}} of {{total}}.',
                    {
                      done: decryptProgress.done,
                      total: decryptProgress.total,
                    }
                  )}
                </p>
              </Callout>
            )}
          </div>
        ) : detailError ? (
          <Callout tone="error">{detailError}</Callout>
        ) : (
          <div className="flex flex-col gap-3">
            <div className="flex items-center justify-between">
              <div className="text-sm font-semibold text-text">
                {detail?.title ||
                  t(
                    'adminTestFeedback.feedback.untitled',
                    'Untitled conversation'
                  )}
              </div>
              {selectedId && (
                <div className="flex items-center gap-2">
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => void handleExport()}
                    disabled={exportDisabled}
                    leadingIcon={
                      exporting ? (
                        <Loader2 className="h-4 w-4 animate-spin" />
                      ) : (
                        <Download className="h-4 w-4" />
                      )
                    }
                  >
                    {t('common.export', 'Export')}
                  </Button>
                  {exportBlockedMessage && (
                    // A disabled button swallows mouse events, so a native
                    // title would never fire and would be invisible to keyboard
                    // users. Use an explicit toggle instead. See #643.
                    <IconButton
                      label={t(
                        'adminTestFeedback.feedback.exportWhyDisabled',
                        'Why is export disabled?'
                      )}
                      onClick={() => setShowExportHelp((open) => !open)}
                      pressed={showExportHelp}
                    >
                      <Info className="h-4 w-4" aria-hidden="true" />
                    </IconButton>
                  )}
                  {exportBlockedReason === 'partial' && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => void openLog(selectedId)}
                      leadingIcon={<RefreshCw className="h-4 w-4" />}
                    >
                      {t(
                        'adminTestFeedback.feedback.retryDecryption',
                        'Retry decryption'
                      )}
                    </Button>
                  )}
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => void handleDelete(selectedId)}
                    leadingIcon={<Trash2 className="h-4 w-4" />}
                  >
                    {t('common.delete', 'Delete')}
                  </Button>
                </div>
              )}
            </div>

            {showExportHelp && exportBlockedMessage && (
              <Callout tone="accent">{exportBlockedMessage}</Callout>
            )}
            {feedbackError && <Callout tone="error">{feedbackError}</Callout>}
            {exportError && <Callout tone="error">{exportError}</Callout>}
            {deleteError && <Callout tone="error">{deleteError}</Callout>}

            {(turns ?? []).map((turn, index) => {
              const canRate = (detail?.transcript_messages.length ?? 0) > 0;
              const draft = drafts[turn.message_id];
              const isAssistant = turn.role === 'assistant';
              return (
                <Card key={turn.message_id} className="flex flex-col gap-2">
                  <div className="flex items-center gap-2">
                    <span
                      className={[
                        'rounded px-1.5 py-0.5 text-xs font-medium',
                        isAssistant
                          ? 'bg-accent/15 text-accent'
                          : 'bg-surface-overlay text-text-secondary',
                      ].join(' ')}
                    >
                      {turn.role}
                    </span>
                    <span className="text-xs text-text-muted">
                      {t('adminTestFeedback.feedback.turn', 'Turn')} {index + 1}
                    </span>
                  </div>
                  <div className="whitespace-pre-wrap break-words text-sm text-text">
                    {turn.content}
                  </div>
                  {isAssistant && <TranscriptTraceSummary turn={turn} />}

                  {/* Only the assistant's (machine) turns are ratable — the
                      user's own messages aren't graded. Logs saved before their
                      message order was recorded are read-only. */}
                  {isAssistant && canRate && (
                    <div className="mt-1 flex flex-wrap items-center gap-2 border-t border-border pt-2">
                      <button
                        onClick={() => void saveFeedback(turn.message_id, 'up')}
                        disabled={draft?.saving}
                        aria-pressed={draft?.rating === 'up'}
                        className={[
                          'inline-flex h-8 w-8 items-center justify-center rounded-lg border transition-colors',
                          draft?.rating === 'up'
                            ? 'border-green-500/50 bg-green-500/15 text-green-400'
                            : 'border-border text-text-muted hover:bg-surface-overlay',
                        ].join(' ')}
                        title={t(
                          'adminTestFeedback.feedback.thumbUp',
                          'Good answer'
                        )}
                      >
                        <ThumbsUp className="h-4 w-4" />
                      </button>
                      <button
                        onClick={() =>
                          void saveFeedback(turn.message_id, 'down')
                        }
                        disabled={draft?.saving}
                        aria-pressed={draft?.rating === 'down'}
                        className={[
                          'inline-flex h-8 w-8 items-center justify-center rounded-lg border transition-colors',
                          draft?.rating === 'down'
                            ? 'border-red-500/50 bg-red-500/15 text-red-400'
                            : 'border-border text-text-muted hover:bg-surface-overlay',
                        ].join(' ')}
                        title={t(
                          'adminTestFeedback.feedback.thumbDown',
                          'Needs work'
                        )}
                      >
                        <ThumbsDown className="h-4 w-4" />
                      </button>
                      <input
                        type="text"
                        value={draft?.comment ?? ''}
                        onChange={(e) =>
                          updateDraft(turn.message_id, {
                            comment: e.target.value,
                          })
                        }
                        onBlur={() => {
                          if (draft?.rating)
                            void saveFeedback(turn.message_id, draft.rating);
                        }}
                        placeholder={t(
                          'adminTestFeedback.feedback.notePlaceholder',
                          'Optional note (saved encrypted)…'
                        )}
                        className="min-w-0 flex-1 rounded-lg border border-border bg-surface px-3 py-1.5 text-sm text-text placeholder:text-text-muted focus:border-accent focus:outline-none"
                      />
                      {draft?.saving ? (
                        <Loader2 className="h-4 w-4 animate-spin text-text-muted" />
                      ) : draft?.saved ? (
                        <span className="text-xs text-text-muted">
                          {t('common.saved', 'Saved')}
                        </span>
                      ) : null}
                    </div>
                  )}
                </Card>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
