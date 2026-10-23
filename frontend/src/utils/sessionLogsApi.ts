/**
 * Typed client for the Session Logs (Test & Feedback) admin endpoints.
 *
 * Transcripts and feedback comments come back as NIP-04 ciphertext encrypted to
 * the admin pubkey; the caller decrypts client-side via NIP-07 (see encryption.ts).
 * The backend never returns plaintext transcript content.
 */

import { adminFetch } from './adminApi';

export type SessionLogSource = 'admin_test' | 'user';
export type FeedbackRating = 'up' | 'down';

export interface TranscriptTrace {
  visibility?: 'off' | 'minimal' | 'summary' | 'detailed' | string | null;
  reasoning?: {
    summary?: string | null;
  } | null;
  tools?: Array<{
    id?: string | null;
    name?: string | null;
    status?: string | null;
    execution?: string | null;
    input_summary?: string | null;
    output_summary?: string | null;
    warnings?: string[];
    metadata?: Record<string, unknown>;
  }>;
  retrieval?: Array<{
    source_type?: string | null;
    title?: string | null;
    summary?: string | null;
    score?: number | null;
    metadata?: Record<string, unknown>;
  }>;
  trace_deltas?: Array<{
    id?: string | null;
    kind?: string | null;
    title?: string | null;
    content?: string | null;
    tool_name?: string | null;
    status?: string | null;
    metadata?: Record<string, unknown>;
    created_at?: string | null;
  }>;
  activity_steps?: Array<{
    id?: string | null;
    kind?: string | null;
    title?: string | null;
    status?: string | null;
    summary?: string | null;
    warnings?: string[];
  }>;
  suppressed?: boolean;
}

export interface TranscriptToolCall {
  tool_id: string;
  tool_name: string;
  query?: string | null;
  output_summary?: string | null;
  warnings: string[];
  guarded: boolean;
}

export interface TranscriptTurn {
  /** Stable ID of this message; feedback is saved against it. */
  message_id?: string | null;
  role: string; // 'user' | 'assistant' | 'system'
  content: string;
  ts?: string | null;
  trace?: TranscriptTrace | null;
  tools_used: TranscriptToolCall[];
}

export interface SessionLogMetadata {
  log_id: string;
  source: SessionLogSource;
  title: string | null;
  subject_user_id: number | null;
  user_type_id: number | null;
  sage_session_id: string | null;
  turn_count: number;
  status: 'active' | 'completed' | 'archived';
  created_by: string | null;
  created_at: string | null;
  updated_at: string | null;
  completed_at: string | null;
  has_transcript: boolean;
}

export interface SessionLogTranscriptMessage {
  message_id: string;
  role: string;
}

export interface SessionLogMessageFeedback {
  message_id: string;
  /** The message's current position, or null if the transcript no longer has it. */
  turn_index: number | null;
  rating: FeedbackRating;
  comment_ciphertext: string | null;
  comment_ephemeral_pubkey: string | null;
  created_by: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface SessionLogDetail extends SessionLogMetadata {
  transcript_ciphertext: string | null;
  transcript_ephemeral_pubkey: string | null;
  encrypted_to_pubkey: string | null;
  /** Plaintext message order of the encrypted transcript. */
  transcript_messages: SessionLogTranscriptMessage[];
  feedback: SessionLogMessageFeedback[];
}

export interface AdminUserType {
  id: number;
  name: string;
  description?: string | null;
  icon?: string | null;
  display_order?: number;
}

export interface TestUserProvision {
  user_id: number;
  user_type_id: number | null;
  created: boolean;
}

export interface ImpersonationToken {
  token: string;
  expires_at?: string;
}

async function jsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body?.detail) detail = body.detail;
    } catch {
      // keep status fallback
    }
    const error = new Error(detail) as Error & { status?: number };
    error.status = res.status;
    throw error;
  }
  return res.json() as Promise<T>;
}

export async function listSessionLogs(params?: {
  source?: SessionLogSource;
  status?: string;
}): Promise<SessionLogMetadata[]> {
  const query = new URLSearchParams();
  if (params?.source) query.set('source', params.source);
  if (params?.status) query.set('status', params.status);
  const suffix = query.toString() ? `?${query.toString()}` : '';
  const data = await jsonOrThrow<{ session_logs: SessionLogMetadata[] }>(
    await adminFetch(`/admin/session-logs${suffix}`)
  );
  return data.session_logs;
}

export async function getSessionLog(logId: string): Promise<SessionLogDetail> {
  return jsonOrThrow<SessionLogDetail>(
    await adminFetch(`/admin/session-logs/${encodeURIComponent(logId)}`)
  );
}

export async function exportSessionLog(logId: string): Promise<Blob> {
  const res = await adminFetch(
    `/admin/session-logs/${encodeURIComponent(logId)}/export`
  );
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body?.detail) detail = body.detail;
    } catch {
      // keep status fallback
    }
    const error = new Error(detail) as Error & { status?: number };
    error.status = res.status;
    throw error;
  }
  return res.blob();
}

export async function recordSessionLogPlaintextExport(
  logId: string
): Promise<void> {
  await jsonOrThrow<{ success: boolean; message: string }>(
    await adminFetch(
      `/admin/session-logs/${encodeURIComponent(logId)}/plaintext-export`,
      { method: 'POST' }
    )
  );
}

export async function createSessionLog(body: {
  source?: SessionLogSource;
  title?: string | null;
  subject_user_id?: number | null;
  user_type_id?: number | null;
  sage_session_id?: string | null;
}): Promise<SessionLogMetadata> {
  return jsonOrThrow<SessionLogMetadata>(
    await adminFetch('/admin/session-logs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
  );
}

export async function saveTranscript(
  logId: string,
  turns: TranscriptTurn[],
  title?: string | null
): Promise<SessionLogMetadata> {
  return jsonOrThrow<SessionLogMetadata>(
    await adminFetch(
      `/admin/session-logs/${encodeURIComponent(logId)}/transcript`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ turns, title: title ?? null }),
      }
    )
  );
}

export async function setMessageFeedback(
  logId: string,
  messageId: string,
  rating: FeedbackRating,
  comment?: string | null
): Promise<SessionLogMessageFeedback> {
  return jsonOrThrow<SessionLogMessageFeedback>(
    await adminFetch(
      `/admin/session-logs/${encodeURIComponent(logId)}/messages/${encodeURIComponent(messageId)}/feedback`,
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rating, comment: comment ?? null }),
      }
    )
  );
}

export async function deleteSessionLog(logId: string): Promise<void> {
  const res = await adminFetch(
    `/admin/session-logs/${encodeURIComponent(logId)}`,
    { method: 'DELETE' }
  );
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
}

export async function listUserTypes(): Promise<AdminUserType[]> {
  // NOTE: /admin/user-types returns { types: [...] }, not { user_types: [...] }.
  const data = await jsonOrThrow<{ types: AdminUserType[] }>(
    await adminFetch('/admin/user-types')
  );
  return data.types ?? [];
}

export async function provisionTestUser(
  userTypeId?: number | null
): Promise<TestUserProvision> {
  const suffix =
    userTypeId != null ? `?user_type_id=${encodeURIComponent(userTypeId)}` : '';
  return jsonOrThrow<TestUserProvision>(
    await adminFetch(`/admin/test-users/provision${suffix}`, { method: 'POST' })
  );
}

/**
 * Whether admin-as-test-user impersonation is wired in this deployment. Lets the
 * UI gate cleanly instead of probing the token endpoint and logging a 501.
 */
export async function getImpersonationStatus(): Promise<boolean> {
  try {
    const data = await jsonOrThrow<{ available: boolean }>(
      await adminFetch('/admin/impersonation/status')
    );
    return Boolean(data.available);
  } catch {
    return false;
  }
}

/**
 * Request a scoped impersonation token to chat AS the test user.
 * Returns null when impersonation is not yet configured (501 from the seam) —
 * callers should surface an honest "not connected" state rather than fail.
 */
export async function requestImpersonationToken(
  userId: number
): Promise<ImpersonationToken | null> {
  const res = await adminFetch(
    `/admin/test-users/${userId}/impersonation-token`,
    { method: 'POST' }
  );
  if (res.status === 501) return null;
  return jsonOrThrow<ImpersonationToken>(res);
}
