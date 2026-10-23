import { describe, expect, it, vi } from 'vitest';
import {
  assertInstrumentationExcludesValues,
  buildProviderFailureInstrumentation,
  emitAdminResilienceInstrumentation,
  registerAdminResilienceInstrumentationListener,
  resetAdminResilienceInstrumentationListeners,
} from './adminResilienceInstrumentation';
import { classifyProviderError } from './providerErrors';

describe('buildProviderFailureInstrumentation', () => {
  it('emits safe provider category and recovery metadata', () => {
    const rawProviderPayload =
      'HttpError: 429 {"error":{"message":"Token limit exceeded for this session. Please start a new session."}}';
    const classified = classifyProviderError(rawProviderPayload);

    const event = buildProviderFailureInstrumentation({
      surface: 'admin_chat_page',
      classified,
    });

    expect(event).toEqual({
      kind: 'provider_failure',
      surface: 'admin_chat_page',
      category: 'context_limit',
      recoveryHint: 'Start a new assistant conversation to continue.',
      retryPolicy: 'never',
      recoveryAction: 'new_assistant_conversation',
    });
    assertInstrumentationExcludesValues(event, [
      rawProviderPayload,
      'HttpError',
      '429',
      'Please start a new session',
    ]);
  });
});

describe('emitAdminResilienceInstrumentation', () => {
  it('delivers sanitized events to registered listeners', () => {
    resetAdminResilienceInstrumentationListeners();
    const listener = vi.fn();
    registerAdminResilienceInstrumentationListener(listener);

    emitAdminResilienceInstrumentation(
      buildProviderFailureInstrumentation({
        surface: 'admin_config_assistant',
        classified: classifyProviderError('Model Provider stream timed out'),
      })
    );

    expect(listener).toHaveBeenCalledTimes(1);
    expect(listener.mock.calls[0]?.[0]).toMatchObject({
      kind: 'provider_failure',
      category: 'timeout',
      recoveryAction: 'none',
    });
    resetAdminResilienceInstrumentationListeners();
  });
});
