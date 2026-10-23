import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  sendLlmChatStreamWithUnifiedTools,
  sendLlmChatWithUnifiedTools,
} from './llmChat';
import { adminFetch } from './adminApi';
import { buildAdminSignerDecryptedContext } from './adminSignerContext';

vi.mock('./adminApi', () => ({
  adminFetch: vi.fn(),
}));

vi.mock('./adminSignerContext', () => ({
  buildAdminSignerDecryptedContext: vi.fn(),
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

  it('sends Admin Signer-Decrypted Context for opted-in Database turns', async () => {
    vi.mocked(buildAdminSignerDecryptedContext).mockResolvedValueOnce({
      source: 'admin-signer-user-roster',
      generated_at: '2026-07-05T22:00:00.000Z',
      users: [
        {
          id: 7,
          approved: false,
          user_type_id: null,
          created_at: '2026-07-03T16:40:00Z',
          pubkey_present: false,
          email: 'marisol@example.test',
          name: 'Marisol Rivera',
        },
      ],
      truncated: false,
      warnings: [],
    });

    await sendLlmChatWithUnifiedTools({
      content: 'Tell me about the users in our db',
      tools: ['db-query'],
      includeAdminSignerDecryptedContext: true,
    });

    const [, options] = vi.mocked(fetch).mock.calls[0];
    expect(JSON.parse(String(options?.body))).toEqual({
      message: 'Tell me about the users in our db',
      tools: ['db-query'],
      client_decrypted_context: {
        source: 'admin-signer-user-roster',
        generated_at: '2026-07-05T22:00:00.000Z',
        users: [
          {
            id: 7,
            approved: false,
            user_type_id: null,
            created_at: '2026-07-03T16:40:00Z',
            pubkey_present: false,
            email: 'marisol@example.test',
            name: 'Marisol Rivera',
          },
        ],
        truncated: false,
        warnings: [],
      },
    });
  });

  it('does not build signer-decrypted context for non-Database turns', async () => {
    await sendLlmChatWithUnifiedTools({
      content: 'Check configuration',
      tools: ['admin-config'],
      includeAdminSignerDecryptedContext: true,
    });

    const [, options] = vi.mocked(fetch).mock.calls[0];
    expect(buildAdminSignerDecryptedContext).not.toHaveBeenCalled();
    expect(JSON.parse(String(options?.body))).toEqual({
      message: 'Check configuration',
      tools: ['admin-config'],
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

    expect(events).toEqual([
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

    expect(events).toHaveLength(1);
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

  it('streams Admin Signer-Decrypted Context for opted-in Database turns', async () => {
    vi.mocked(buildAdminSignerDecryptedContext).mockResolvedValueOnce({
      source: 'admin-signer-user-roster',
      generated_at: '2026-07-05T22:00:00.000Z',
      users: [
        {
          id: 5,
          approved: true,
          user_type_id: 1,
          created_at: '2026-07-01T21:22:00Z',
          pubkey_present: true,
          email: 'ana@example.test',
        },
      ],
      truncated: false,
      warnings: [],
    });
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
      content: 'Tell me about the users',
      tools: ['db-query'],
      includeAdminSignerDecryptedContext: true,
      onEvent: vi.fn(),
    });

    const [, options] = vi.mocked(fetch).mock.calls[0];
    expect(JSON.parse(String(options?.body))).toMatchObject({
      message: 'Tell me about the users',
      tools: ['db-query'],
      client_decrypted_context: {
        source: 'admin-signer-user-roster',
        users: [{ id: 5, email: 'ana@example.test' }],
      },
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

    expect(events).toEqual([
      { event: 'answer_delta', data: { message_id: 'msg_1', delta: 'Hello' } },
    ]);
  });
});
