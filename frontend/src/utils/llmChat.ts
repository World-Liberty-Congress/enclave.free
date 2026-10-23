import { API_BASE } from '../types/onboarding';
import {
  classifyProviderError,
  formatClassifiedProviderError,
} from './providerErrors';
import {
  createUserLookupHandler,
  parseLookupRequest,
  isAdminAuthStorageEvent,
} from './adminUserLookup';
import { hasNip04Support } from './encryption';

interface SendLlmChatOptions {
  content: string;
  tools: string[];
  conversationSurface?: 'admin-onboarding';
  sessionId?: string | null;
  jobIds?: string[];
  /**
   * Optional bearer token to authenticate AS a specific user (impersonation).
   * Sage prefers the Authorization bearer over the session cookie, so this scopes
   * just this request to the token's user without touching the admin cookie.
   */
  authToken?: string | null;
  /** Advertise browser capability without requesting signer permission. */
  adminUserLookup?: boolean;
  signal?: AbortSignal;
}

interface SendLlmChatStreamOptions extends SendLlmChatOptions {
  onEvent: (event: string, data: unknown) => void;
}

function buildUnifiedChatBody(
  options: SendLlmChatOptions
): Record<string, unknown> {
  const { content, tools, conversationSurface, sessionId, jobIds } = options;
  return {
    message: content,
    tools,
    ...(conversationSurface
      ? { conversation_surface: conversationSurface }
      : {}),
    ...(sessionId ? { session_id: sessionId } : {}),
    ...(jobIds?.filter(Boolean).length
      ? { job_ids: jobIds.filter(Boolean) }
      : {}),
  };
}

/** A dispatched stream must never be replayed automatically as another turn. */
export class StreamTurnError extends Error {
  constructor(
    message: string,
    readonly streamStarted: boolean
  ) {
    super(message);
  }
}

export async function sendLlmChatWithUnifiedTools(
  options: SendLlmChatOptions
): Promise<Response> {
  const body = buildUnifiedChatBody(options);

  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
  };
  if (options.authToken) {
    headers.Authorization = `Bearer ${options.authToken}`;
  }

  return fetch(`${API_BASE}/llm/chat`, {
    method: 'POST',
    headers,
    credentials: 'include',
    body: JSON.stringify(body),
    signal: options.signal,
  });
}

export async function sendLlmChatStreamWithUnifiedTools({
  onEvent,
  ...options
}: SendLlmChatStreamOptions): Promise<void> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  const onStorage = (event: StorageEvent) => {
    if (isAdminAuthStorageEvent(event)) abort();
  };
  options.signal?.addEventListener('abort', abort, { once: true });
  window.addEventListener('enclave:admin-auth-cleared', abort);
  window.addEventListener('storage', onStorage);
  if (options.signal?.aborted) abort();
  const body = buildUnifiedChatBody(options);
  const nonce = crypto.randomUUID();
  const enabled =
    !!options.adminUserLookup &&
    !options.authToken &&
    options.tools.includes('db-query') &&
    hasNip04Support();
  if (enabled) body.user_lookup = { version: 1, nonce };
  const lookup = enabled
    ? createUserLookupHandler({
        nonce,
        signal: controller.signal,
        onProgress: (scanned) =>
          onEvent('trace_status', {
            status: `Searching Users: ${scanned} scanned.`,
            phase: 'user_lookup',
          }),
      })
    : null;
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    Accept: 'text/event-stream',
  };
  if (options.authToken) headers.Authorization = `Bearer ${options.authToken}`;
  let started = false;
  let completed = false;
  let asynchronousFailure: unknown;
  const calls = new Map<string, string>();
  const pending = new Set<Promise<void>>();
  try {
    // A network error after dispatch cannot establish that the server did not start.
    started = true;
    const response = await fetch(`${API_BASE}/llm/chat/stream`, {
      method: 'POST',
      headers,
      credentials: 'include',
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    if (!response.ok) {
      started = ![404, 405, 501].includes(response.status);
      throw new Error(await responseErrorMessage(response));
    }
    await readSseResponse(
      response,
      (event, data) => {
        if (event === 'user_lookup_request') {
          if (!lookup) throw new Error('Unexpected browser lookup request');
          const request = parseLookupRequest(data, nonce);
          const encoded = JSON.stringify(request);
          const prior = calls.get(request.call_id);
          if (prior) {
            if (prior !== encoded)
              throw new Error('Conflicting browser lookup request');
            return;
          }
          if (calls.size >= 128)
            throw new Error('Browser lookup limit reached');
          calls.set(request.call_id, encoded);
          const task = (async () => {
            const result = await lookup.handle(request);
            controller.signal.throwIfAborted();
            const reply = await fetch(
              `${API_BASE}/llm/chat/user-lookup-result`,
              {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                signal: controller.signal,
                body: JSON.stringify({
                  conversation_id: request.conversation_id,
                  turn_id: request.turn_id,
                  call_id: request.call_id,
                  nonce,
                  result,
                }),
              }
            );
            if (!reply.ok)
              throw new Error('Browser lookup result was not accepted');
          })().catch(() => {
            asynchronousFailure = new Error(
              'Browser lookup interrupted. This turn was not replayed.'
            );
            controller.abort();
          });
          pending.add(task);
          void task.finally(() => pending.delete(task));
          return;
        }
        if (event === 'done') completed = true;
        onEvent(event, data);
        if (event === 'error')
          throw new Error(
            extractProviderDetail(data) || 'Streaming turn failed'
          );
      },
      controller.signal
    );
    if (asynchronousFailure) throw asynchronousFailure;
    if (!completed)
      throw new Error(
        'The stream ended before the turn completed. This turn was not replayed.'
      );
  } catch (error) {
    throw new StreamTurnError(
      error instanceof Error ? error.message : 'Streaming turn failed',
      started
    );
  } finally {
    controller.abort();
    lookup?.dispose();
    calls.clear();
    options.signal?.removeEventListener('abort', abort);
    window.removeEventListener('enclave:admin-auth-cleared', abort);
    window.removeEventListener('storage', onStorage);
    await Promise.allSettled([...pending]);
  }
}

async function responseErrorMessage(response: Response): Promise<string> {
  const fallback = `HTTP ${response.status}`;
  try {
    const contentType = response.headers.get('content-type') || '';
    if (contentType.includes('application/json')) {
      const data = (await response.json()) as Record<string, unknown>;
      return formatProviderError(data, fallback);
    }
    const text = (await response.text()).trim();
    return formatProviderError(text, text || fallback);
  } catch {
    return fallback;
  }
}

function formatProviderError(raw: unknown, fallback: string): string {
  const classified = classifyProviderError(raw);
  if (classified.category === 'unknown' && !extractProviderDetail(raw)) {
    return fallback;
  }
  return formatClassifiedProviderError(classified);
}

function extractProviderDetail(raw: unknown): string {
  if (typeof raw === 'string') {
    return raw.trim();
  }
  if (raw && typeof raw === 'object') {
    const record = raw as Record<string, unknown>;
    for (const key of ['detail', 'message', 'error'] as const) {
      const value = record[key];
      if (typeof value === 'string' && value.trim()) {
        return value.trim();
      }
    }
  }
  return '';
}

async function readSseResponse(
  response: Response,
  onEvent: (event: string, data: unknown) => void,
  signal: AbortSignal
): Promise<void> {
  if (!response.body) {
    throw new Error('Streaming response body is unavailable');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  const cancel = () => {
    void reader.cancel().catch(() => {});
  };
  signal.addEventListener('abort', cancel, { once: true });
  try {
    while (true) {
      signal.throwIfAborted();
      const { done, value } = await reader.read();
      signal.throwIfAborted();
      if (done) break;
      buffer += normalizeSseNewlines(decoder.decode(value, { stream: true }));
      if (buffer.length > 1_000_000) throw new Error('Invalid streaming frame');
      buffer = drainSseBuffer(buffer, onEvent);
    }
    buffer += normalizeSseNewlines(decoder.decode());
    drainSseBuffer(`${buffer}\n\n`, onEvent);
  } finally {
    signal.removeEventListener('abort', cancel);
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

function normalizeSseNewlines(value: string): string {
  return value.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
}

function drainSseBuffer(
  buffer: string,
  onEvent: (event: string, data: unknown) => void
): string {
  let nextBuffer = buffer;
  let boundary = nextBuffer.indexOf('\n\n');

  while (boundary !== -1) {
    const rawEvent = nextBuffer.slice(0, boundary);
    nextBuffer = nextBuffer.slice(boundary + 2);
    dispatchSseEvent(rawEvent, onEvent);
    boundary = nextBuffer.indexOf('\n\n');
  }

  return nextBuffer;
}

function dispatchSseEvent(
  rawEvent: string,
  onEvent: (event: string, data: unknown) => void
) {
  const lines = rawEvent.split('\n');
  const eventLine = lines.find((line) => line.startsWith('event:'));
  const dataLines = lines
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.slice('data:'.length).trimStart());

  if (!eventLine) return;

  const event = eventLine.slice('event:'.length).trim();
  const rawData = dataLines.join('\n');
  let data: unknown = rawData;
  if (rawData) {
    try {
      data = JSON.parse(rawData);
    } catch {
      data = rawData;
    }
  }
  onEvent(event, data);
}
