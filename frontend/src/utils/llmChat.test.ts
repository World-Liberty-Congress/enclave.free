import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  sendLlmChatStreamWithUnifiedTools,
  sendLlmChatWithUnifiedTools,
} from './llmChat';
import { adminFetch } from './adminApi';

vi.mock('./adminApi', () => ({
  adminFetch: vi.fn(),
}));

describe('sendLlmChatWithUnifiedTools', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(Response.json({ message: 'ok' }))
    );
  });

  it('sends the conversation session id on memory-backed chat turns', async () => {
    await sendLlmChatWithUnifiedTools({
      content: 'What was the secret word?',
      tools: [],
      sessionId: 'session-123',
    });

    expect(fetch).toHaveBeenCalledWith(
      '/api/llm/chat',
      expect.objectContaining({
        method: 'POST',
        credentials: 'include',
        body: expect.any(String),
      })
    );
    const [, options] = vi.mocked(fetch).mock.calls[0];
    expect(JSON.parse(String(options?.body))).toEqual({
      message: 'What was the secret word?',
      tools: [],
      session_id: 'session-123',
    });
  });

  it('omits session_id when no conversation session id is provided', async () => {
    await sendLlmChatWithUnifiedTools({
      content: 'Start fresh',
      tools: [],
    });

    expect(fetch).toHaveBeenCalledWith(
      '/api/llm/chat',
      expect.objectContaining({
        method: 'POST',
        credentials: 'include',
        body: expect.any(String),
      })
    );
    const [, options] = vi.mocked(fetch).mock.calls[0];
    expect(JSON.parse(String(options?.body))).not.toHaveProperty('session_id');
  });

  it('sends admin-config as a backend tool instead of requiring client tool context', async () => {
    await sendLlmChatWithUnifiedTools({
      content: 'Check my deployment config',
      tools: ['admin-config'],
    });

    const [, options] = vi.mocked(fetch).mock.calls[0];
    expect(JSON.parse(String(options?.body))).toEqual({
      message: 'Check my deployment config',
      tools: ['admin-config'],
    });
  });

  it('sends the guided onboarding surface without altering the user message', async () => {
    await sendLlmChatWithUnifiedTools({
      content: '1. FreeThem, 4. blue',
      tools: ['admin-config'],
      conversationSurface: 'admin-onboarding',
    });

    const [, options] = vi.mocked(fetch).mock.calls[0];
    expect(JSON.parse(String(options?.body))).toEqual({
      message: '1. FreeThem, 4. blue',
      tools: ['admin-config'],
      conversation_surface: 'admin-onboarding',
    });
  });

  it('sends Knowledge Search document constraints through unified chat', async () => {
    await sendLlmChatWithUnifiedTools({
      content: 'Use the uploaded handbook',
      tools: ['knowledge-search'],
      jobIds: ['doc-handbook', 'doc-faq'],
    });

    const [, options] = vi.mocked(fetch).mock.calls[0];
    expect(JSON.parse(String(options?.body))).toEqual({
      message: 'Use the uploaded handbook',
      tools: ['knowledge-search'],
      job_ids: ['doc-handbook', 'doc-faq'],
    });
  });

  it('does not send frontend-prepared tool context fields', async () => {
    await sendLlmChatWithUnifiedTools({
      content: 'Use the database tool',
      tools: ['db-query'],
    });

    const [, options] = vi.mocked(fetch).mock.calls[0];
    const body = JSON.parse(String(options?.body));
    expect(body).toEqual({
      message: 'Use the database tool',
      tools: ['db-query'],
    });
  });

  it('streams assistant message lifecycle events from the chat stream endpoint', async () => {
    const encoder = new TextEncoder();
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          new ReadableStream({
            start(controller) {
              controller.enqueue(
                encoder.encode(
                  'event: assistant_message_started\ndata: {"message_id":"msg_1","session_id":"s1"}\n\n'
                )
              );
              controller.enqueue(
                encoder.encode(
                  'event: trace_status\ndata: {"message_id":"msg_1","status":"Using Web search"}\n\n'
                )
              );
              controller.enqueue(
                encoder.encode(
                  'event: answer_delta\ndata: {"message_id":"msg_1","delta":"Hello"}\n\n'
                )
              );
              controller.enqueue(
                encoder.encode(
                  'event: trace_final\ndata: {"message_id":"msg_1","trace":{"visibility":"minimal","reasoning":{"summary":"Sage answered."},"tools":[],"retrieval":[]}}\n\n'
                )
              );
              controller.enqueue(
                encoder.encode(
                  'event: done\ndata: {"message_id":"msg_1","session_id":"s1","inference_verification":{"record_id":42}}\n\n'
                )
              );
              controller.close();
            },
          }),
          { headers: { 'Content-Type': 'text/event-stream' } }
        )
      )
    );
    const events: Array<{ event: string; data: unknown }> = [];

    await sendLlmChatStreamWithUnifiedTools({
      content: 'Hello',
      tools: [],
      onEvent: (event, data) => events.push({ event, data }),
    });

    expect(fetch).toHaveBeenCalledWith(
      '/api/llm/chat/stream',
      expect.objectContaining({
        method: 'POST',
        credentials: 'include',
        body: expect.any(String),
      })
    );
    expect(events.map((event) => event.event)).toEqual([
      'assistant_message_started',
      'trace_status',
      'answer_delta',
      'trace_final',
      'done',
    ]);
    expect(events[1].data).toEqual({
      message_id: 'msg_1',
      status: 'Using Web search',
    });
    expect(events[2].data).toEqual({ message_id: 'msg_1', delta: 'Hello' });
  });

  it('sends impersonation bearer auth on streamed chat turns', async () => {
    const encoder = new TextEncoder();
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          new ReadableStream({
            start(controller) {
              controller.enqueue(
                encoder.encode('event: done\ndata: {"message_id":"msg_1"}\n\n')
              );
              controller.close();
            },
          }),
          { headers: { 'Content-Type': 'text/event-stream' } }
        )
      )
    );

    await sendLlmChatStreamWithUnifiedTools({
      content: 'Hello as synthetic user',
      tools: [],
      authToken: 'synthetic-user-token',
      onEvent: vi.fn(),
    });

    expect(fetch).toHaveBeenCalledWith(
      '/api/llm/chat/stream',
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: 'Bearer synthetic-user-token',
        }),
      })
    );
  });

  it('streams Conversation Activity Step events from the chat stream endpoint', async () => {
    const encoder = new TextEncoder();
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          new ReadableStream({
            start(controller) {
              controller.enqueue(
                encoder.encode(
                  [
                    'event: activity_step',
                    'data: {"message_id":"msg_1","activity_step":{"id":"tool-db-query","kind":"tool","title":"Database Query","status":"succeeded","summary":"Database results were redacted from the trace.","warnings":["raw_results_redacted"]}}',
                    '',
                    '',
                  ].join('\n')
                )
              );
              controller.enqueue(encoder.encode('event: done\ndata: {}\n\n'));
              controller.close();
            },
          }),
          { headers: { 'Content-Type': 'text/event-stream' } }
        )
      )
    );
    const events: Array<{ event: string; data: unknown }> = [];

    await sendLlmChatStreamWithUnifiedTools({
      content: 'Check settings',
      tools: ['db-query'],
      onEvent: (event, data) => events.push({ event, data }),
    });

    expect(events.filter((e) => e.event !== 'done')).toEqual([
      {
        event: 'activity_step',
        data: {
          message_id: 'msg_1',
          activity_step: {
            id: 'tool-db-query',
            kind: 'tool',
            title: 'Database Query',
            status: 'succeeded',
            summary: 'Database results were redacted from the trace.',
            warnings: ['raw_results_redacted'],
          },
        },
      },
    ]);
  });

  it('delivers timing deltas through the stream without opening an Audit Log write path', async () => {
    const encoder = new TextEncoder();
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          new ReadableStream({
            start(controller) {
              controller.enqueue(
                encoder.encode(
                  [
                    'event: trace_delta',
                    'data: {"message_id":"msg_timing","trace_delta":{"id":"timing-1","kind":"timing","title":"Model request","content":"Model request: 842 ms.","status":"succeeded","metadata":{"phase":"model_request","step":1,"attempt":2,"outcome":"succeeded","duration_ms":842,"provider_wait_proxy":false}}}',
                    '',
                    '',
                  ].join('\n')
                )
              );
              controller.enqueue(encoder.encode('event: done\ndata: {}\n\n'));
              controller.close();
            },
          }),
          { headers: { 'Content-Type': 'text/event-stream' } }
        )
      )
    );
    const events: Array<{ event: string; data: unknown }> = [];

    await sendLlmChatStreamWithUnifiedTools({
      content: 'Show timing',
      tools: [],
      onEvent: (event, data) => events.push({ event, data }),
    });

    expect(events).toHaveLength(2);
    expect(events[0]).toMatchObject({
      event: 'trace_delta',
      data: {
        trace_delta: {
          kind: 'timing',
          metadata: {
            phase: 'model_request',
            step: 1,
            attempt: 2,
          },
        },
      },
    });
    const urls = vi.mocked(fetch).mock.calls.map(([input]) => String(input));
    expect(urls).toEqual(['/api/llm/chat/stream']);
    expect(urls.some((url) => url.includes('audit'))).toBe(false);
  });

  it('streams admin-config as an explicit backend tool without requiring client context', async () => {
    const encoder = new TextEncoder();
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          new ReadableStream({
            start(controller) {
              controller.enqueue(
                encoder.encode('event: done\ndata: {"message_id":"msg_1"}\n\n')
              );
              controller.close();
            },
          }),
          { headers: { 'Content-Type': 'text/event-stream' } }
        )
      )
    );

    await sendLlmChatStreamWithUnifiedTools({
      content: 'Check my deployment config',
      tools: ['admin-config'],
      onEvent: vi.fn(),
    });

    const [, options] = vi.mocked(fetch).mock.calls[0];
    expect(JSON.parse(String(options?.body))).toEqual({
      message: 'Check my deployment config',
      tools: ['admin-config'],
    });
  });

  it('includes classified provider details when streamed chat returns a non-OK status', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        Response.json(
          {
            detail:
              'Token limit exceeded for this session. Please start a new session.',
          },
          { status: 429 }
        )
      )
    );

    await expect(
      sendLlmChatStreamWithUnifiedTools({
        content: 'Apply them',
        tools: ['admin-config'],
        onEvent: vi.fn(),
      })
    ).rejects.toThrow(
      'Token limit exceeded for this session. Start a new assistant conversation to continue.'
    );
  });

  it('classifies timeout failures from streamed chat responses', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(
          Response.json(
            { detail: 'Model Provider stream timed out waiting for data' },
            { status: 504 }
          )
        )
    );

    await expect(
      sendLlmChatStreamWithUnifiedTools({
        content: 'Apply them',
        tools: ['admin-config'],
        onEvent: vi.fn(),
      })
    ).rejects.toThrow(
      'The Model Provider took too long to respond. Try again in a moment.'
    );
  });

  it.each([
    ['a first Admin turn', null],
    ['a continuing Admin turn', 'session-123'],
  ])(
    'sends only the new message on %s because Sage owns the Conversation history',
    async (_label, sessionId) => {
      const encoder = new TextEncoder();
      vi.stubGlobal(
        'fetch',
        vi
          .fn()
          .mockResolvedValueOnce(
            new Response(
              new ReadableStream({
                start(controller) {
                  controller.enqueue(
                    encoder.encode(
                      'event: done\ndata: {"message_id":"msg_1"}\n\n'
                    )
                  );
                  controller.close();
                },
              }),
              { headers: { 'Content-Type': 'text/event-stream' } }
            )
          )
          .mockResolvedValueOnce(Response.json({ message: 'ok' }))
      );

      await sendLlmChatStreamWithUnifiedTools({
        content: 'your suggestions above',
        tools: ['admin-config'],
        conversationSurface: 'admin-onboarding',
        sessionId,
        onEvent: vi.fn(),
      });
      await sendLlmChatWithUnifiedTools({
        content: 'your suggestions above',
        tools: ['admin-config'],
        conversationSurface: 'admin-onboarding',
        sessionId,
      });

      const calls = vi.mocked(fetch).mock.calls;
      expect(calls.map(([url]) => String(url))).toEqual([
        '/api/llm/chat/stream',
        '/api/llm/chat',
      ]);
      for (const [, options] of calls) {
        const body = JSON.parse(String(options?.body));
        expect(body).toEqual({
          message: 'your suggestions above',
          tools: ['admin-config'],
          conversation_surface: 'admin-onboarding',
          ...(sessionId ? { session_id: sessionId } : {}),
        });
        expect(body).not.toHaveProperty('conversation_history');
      }
    }
  );

  it('streams database questions as Sage-owned tool turns without client-executing /admin/tools/execute', async () => {
    vi.mocked(adminFetch).mockResolvedValue(
      Response.json({
        success: true,
        data: {
          sql: 'SELECT encrypted_value, ephemeral_pubkey FROM settings',
          columns: ['encrypted_value', 'ephemeral_pubkey'],
          rows: [{ encrypted_value: 'ciphertext', ephemeral_pubkey: 'pubkey' }],
          row_count: 1,
        },
      })
    );
    const encoder = new TextEncoder();
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          new ReadableStream({
            start(controller) {
              controller.enqueue(
                encoder.encode('event: done\ndata: {"message_id":"msg_1"}\n\n')
              );
              controller.close();
            },
          }),
          { headers: { 'Content-Type': 'text/event-stream' } }
        )
      )
    );

    await sendLlmChatStreamWithUnifiedTools({
      content: 'What is in settings?',
      tools: ['db-query'],
      onEvent: vi.fn(),
    });

    const [, options] = vi.mocked(fetch).mock.calls[0];
    expect(adminFetch).not.toHaveBeenCalled();
    expect(JSON.parse(String(options?.body))).toEqual({
      message: 'What is in settings?',
      tools: ['db-query'],
    });
  });

  it('parses stream events split across chunks with CRLF boundaries', async () => {
    const encoder = new TextEncoder();
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          new ReadableStream({
            start(controller) {
              controller.enqueue(encoder.encode('event: answer_delta\r\n'));
              controller.enqueue(
                encoder.encode('data: {"message_id":"msg_1","delta":"Hel')
              );
              controller.enqueue(encoder.encode('lo"}\r\n\r\n'));
              controller.enqueue(encoder.encode('event: done\ndata: {}\n\n'));
              controller.close();
            },
          }),
          { headers: { 'Content-Type': 'text/event-stream' } }
        )
      )
    );
    const events: Array<{ event: string; data: unknown }> = [];

    await sendLlmChatStreamWithUnifiedTools({
      content: 'Hello',
      tools: [],
      onEvent: (event, data) => events.push({ event, data }),
    });

    expect(events.filter((e) => e.event !== 'done')).toEqual([
      { event: 'answer_delta', data: { message_id: 'msg_1', delta: 'Hello' } },
    ]);
  });
});

it('advertises a nonce without asking the signer, and never advertises impersonation or nonstream capability', async () => {
  const getPublicKey = vi.fn();
  window.nostr = {
    getPublicKey,
    nip04: { decrypt: vi.fn() },
  } as unknown as NonNullable<Window['nostr']>;
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response('event: done\ndata: {}\n\n'))
  );
  await sendLlmChatStreamWithUnifiedTools({
    content: 'users',
    tools: ['db-query'],
    adminUserLookup: true,
    onEvent: vi.fn(),
  });
  expect(
    JSON.parse(String(vi.mocked(fetch).mock.calls[0][1]?.body)).user_lookup
  ).toEqual({ version: 1, nonce: expect.any(String) });
  expect(getPublicKey).not.toHaveBeenCalled();
  await sendLlmChatStreamWithUnifiedTools({
    content: 'users',
    tools: ['db-query'],
    adminUserLookup: true,
    authToken: 'impersonation',
    onEvent: vi.fn(),
  });
  await sendLlmChatWithUnifiedTools({
    content: 'users',
    tools: ['db-query'],
    adminUserLookup: true,
  });
  for (const call of vi.mocked(fetch).mock.calls.slice(1))
    expect(JSON.parse(String(call[1]?.body))).not.toHaveProperty('user_lookup');
});

it('marks even text-free started streams and truncated EOF as non-replayable', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(
      async () =>
        new Response(
          'event: assistant_message_started\ndata: {"message_id":"m"}\n\n'
        )
    )
  );
  await expect(
    sendLlmChatStreamWithUnifiedTools({
      content: 'users',
      tools: ['db-query'],
      onEvent: vi.fn(),
    })
  ).rejects.toMatchObject({ streamStarted: true });
  expect(fetch).toHaveBeenCalledTimes(1);
});

it('rejects malformed dedicated lookup frames without posting or invoking the signer', async () => {
  const getPublicKey = vi.fn();
  window.nostr = {
    getPublicKey,
    nip04: { decrypt: vi.fn() },
  } as unknown as NonNullable<Window['nostr']>;
  vi.stubGlobal(
    'fetch',
    vi.fn(
      async () =>
        new Response(
          'event: user_lookup_request\ndata: {"url":"https://foreign.invalid"}\n\n'
        )
    )
  );
  await expect(
    sendLlmChatStreamWithUnifiedTools({
      content: 'users',
      tools: ['db-query'],
      adminUserLookup: true,
      onEvent: vi.fn(),
    })
  ).rejects.toMatchObject({ streamStarted: true });
  expect(getPublicKey).not.toHaveBeenCalled();
  expect(fetch).toHaveBeenCalledTimes(1);
});

it('continues reading status while signer awaits and posts only the correlated bounded result', async () => {
  const pubkey = 'a'.repeat(64);
  const session_fingerprint = 'b'.repeat(64);
  let decryptResolve!: (value: string) => void;
  const decrypt = vi.fn(
    () =>
      new Promise<string>((resolve) => {
        decryptResolve = resolve;
      })
  );
  window.nostr = {
    getPublicKey: vi.fn(async () => pubkey),
    nip04: { decrypt },
  } as unknown as NonNullable<Window['nostr']>;
  let stream!: ReadableStreamDefaultController<Uint8Array>;
  const encoder = new TextEncoder();
  let nonce = '';
  const received: unknown[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/stream')) {
        nonce = JSON.parse(String(init?.body)).user_lookup.nonce;
        return new Response(
          new ReadableStream({
            start(c) {
              stream = c;
              c.enqueue(
                encoder.encode(
                  `event: user_lookup_request\ndata: ${JSON.stringify({ version: 1, nonce, conversation_id: 'c', turn_id: 't', call_id: '11111111-1111-4111-8111-111111111111', admin_pubkey: pubkey, session_fingerprint, expires_at: Date.now() + 120000, operation: 'read_users', args: { user_ids: [999], select: ['name'] } })}\n\n`
                )
              );
            },
          })
        );
      }
      if (String(input).endsWith('/session'))
        return Response.json({ pubkey, session_fingerprint });
      if (String(input).endsWith('/projection'))
        return Response.json({
          users: [
            {
              id: 999,
              values: {
                name: { ciphertext: 'secret', ephemeral_pubkey: pubkey },
              },
            },
          ],
          fields: [{ selector: 'name', type: 'text' }],
          through_id: 999,
          next_after_id: null,
          eligible: 1,
        });
      received.push(JSON.parse(String(init?.body)));
      stream.enqueue(encoder.encode('event: done\ndata: {}\n\n'));
      stream.close();
      return Response.json({ accepted: true });
    })
  );
  const onEvent = vi.fn();
  const turn = sendLlmChatStreamWithUnifiedTools({
    content: 'users',
    tools: ['db-query'],
    adminUserLookup: true,
    onEvent,
  });
  await vi.waitFor(() => expect(decrypt).toHaveBeenCalledTimes(1));
  stream.enqueue(
    encoder.encode(
      'event: trace_status\ndata: {"status":"Waiting for signer"}\n\n'
    )
  );
  await vi.waitFor(() =>
    expect(onEvent).toHaveBeenCalledWith('trace_status', {
      status: 'Waiting for signer',
    })
  );
  decryptResolve('Synthetic Name');
  await turn;
  expect(received).toEqual([
    expect.objectContaining({
      conversation_id: 'c',
      turn_id: 't',
      nonce,
      call_id: '11111111-1111-4111-8111-111111111111',
      result: expect.objectContaining({
        users: [
          {
            id: 999,
            status: 'ok',
            fields: {
              name: { status: 'ok', value: 'Synthetic Name', excerpt: false },
            },
          },
        ],
      }),
    }),
  ]);
  expect(
    onEvent.mock.calls.some(([name]) => name === 'user_lookup_request')
  ).toBe(false);
});

it('dispatches Conversation Review through the shared stream without exposing plaintext in activity events', async () => {
  const pubkey = 'a'.repeat(64);
  window.nostr = {
    signEvent: vi.fn(),
    getPublicKey: async () => pubkey,
    nip04: {
      encrypt: vi.fn(),
      decrypt: async () =>
        JSON.stringify({
          turns: [
            {
              message_id: 'm1',
              role: 'user',
              content: 'PRIVATE REVIEW TEXT',
              ts: '2026-10-20T10:00:00Z',
            },
          ],
        }),
    },
  } as NonNullable<Window['nostr']>;
  let stream!: ReadableStreamDefaultController<Uint8Array>;
  let sent: Record<string, unknown> | undefined;
  let posted: Record<string, unknown> | undefined;
  const encoder = new TextEncoder();
  const onEvent = vi.fn();
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/stream')) {
        sent = JSON.parse(String(init?.body));
        const capability = sent!.conversation_lookup as
          | { nonce: string }
          | undefined;
        return new Response(
          new ReadableStream({
            start(c) {
              stream = c;
              const request = {
                version: 1,
                conversation_id: 'admin-chat',
                turn_id: 'turn',
                call_id: crypto.randomUUID(),
                nonce: capability?.nonce,
                admin_pubkey: pubkey,
                session_fingerprint: 'b'.repeat(64),
                expires_at: Date.now() + 120000,
                operation: 'read_conversations',
                args: { select: 'transcript' },
              };
              c.enqueue(
                encoder.encode(
                  `event: conversation_lookup_request\ndata: ${JSON.stringify(request)}\n\n`
                )
              );
              // Capability absence is the pre-implementation failure; do not hang.
              if (!capability) c.close();
            },
          })
        );
      }
      if (url.endsWith('/session'))
        return Response.json({ pubkey, session_fingerprint: 'b'.repeat(64) });
      if (url.endsWith('/projection'))
        return Response.json({
          logs: [
            {
              id: 1,
              conversation_id: 'review-1',
              user_id: 5,
              encrypted_to_pubkey: pubkey,
              status: 'ok',
              transcript: {
                ciphertext: 'encrypted',
                ephemeral_pubkey: 'c'.repeat(64),
              },
              feedback: [],
            },
          ],
          through_id: 1,
          eligible_logs: 1,
          next_after_id: null,
          time_basis: 'message_ts',
        });
      if (url.endsWith('/conversation-lookup-result')) {
        posted = JSON.parse(String(init?.body));
        stream.enqueue(encoder.encode('event: done\ndata: {}\n\n'));
        stream.close();
        return Response.json({ status: 'ok' });
      }
      throw new Error('Unexpected route');
    })
  );
  await sendLlmChatStreamWithUnifiedTools({
    content: 'Review conversations',
    tools: ['db-query'],
    adminUserLookup: true,
    onEvent,
  });
  expect(sent).toHaveProperty('conversation_lookup.version', 1);
  expect(posted).toHaveProperty('result.records.0.text', 'PRIVATE REVIEW TEXT');
  expect(JSON.stringify(onEvent.mock.calls)).not.toContain(
    'PRIVATE REVIEW TEXT'
  );
  expect(
    onEvent.mock.calls.some(
      ([event]) => event === 'conversation_lookup_request'
    )
  ).toBe(false);
});
