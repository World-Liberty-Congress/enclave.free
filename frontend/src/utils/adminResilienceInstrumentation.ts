/**
 * Sanitized observability for Admin Configuration Assistant resilience paths.
 *
 * Emits structured metadata about Model Provider failures without raw prompts,
 * secrets, or provider traces.
 */

import type { ClassifiedProviderError } from './providerErrors';
import { shouldOfferNewAssistantConversation } from './providerErrors';

export type AdminResilienceSurface =
  | 'admin_config_assistant'
  | 'admin_chat_page';

export type AdminResilienceInstrumentationKind = 'provider_failure';

export interface ProviderFailureInstrumentation {
  kind: 'provider_failure';
  surface: AdminResilienceSurface;
  category: ClassifiedProviderError['category'];
  recoveryHint: string | null;
  retryPolicy: ClassifiedProviderError['retryPolicy'];
  recoveryAction: 'new_assistant_conversation' | 'none';
}

export type AdminResilienceInstrumentationEvent =
  ProviderFailureInstrumentation;

const instrumentationListeners = new Set<
  (event: AdminResilienceInstrumentationEvent) => void
>();

/**
 * Register a listener for sanitized admin resilience instrumentation events.
 */
export function registerAdminResilienceInstrumentationListener(
  listener: (event: AdminResilienceInstrumentationEvent) => void
): () => void {
  instrumentationListeners.add(listener);
  return () => instrumentationListeners.delete(listener);
}

/**
 * Clear all instrumentation listeners. Intended for tests.
 */
export function resetAdminResilienceInstrumentationListeners(): void {
  instrumentationListeners.clear();
}

/**
 * Build sanitized metadata for a classified Model Provider failure.
 */
export function buildProviderFailureInstrumentation(input: {
  surface: AdminResilienceSurface;
  classified: ClassifiedProviderError;
}): ProviderFailureInstrumentation {
  return {
    kind: 'provider_failure',
    surface: input.surface,
    category: input.classified.category,
    recoveryHint: input.classified.recoveryHint,
    retryPolicy: input.classified.retryPolicy,
    recoveryAction: shouldOfferNewAssistantConversation(input.classified)
      ? 'new_assistant_conversation'
      : 'none',
  };
}

/**
 * Record a sanitized provider failure event for maintainers.
 */
export function recordProviderFailureInstrumentation(input: {
  surface: AdminResilienceSurface;
  classified: ClassifiedProviderError;
}): void {
  emitAdminResilienceInstrumentation(
    buildProviderFailureInstrumentation(input)
  );
}

/**
 * Emit a sanitized admin resilience instrumentation event.
 */
export function emitAdminResilienceInstrumentation(
  event: AdminResilienceInstrumentationEvent
): void {
  for (const listener of instrumentationListeners) {
    listener(event);
  }
}

/**
 * Assert instrumentation payloads exclude sensitive or raw prompt substrings.
 */
export function assertInstrumentationExcludesValues(
  event: AdminResilienceInstrumentationEvent,
  forbiddenValues: string[]
): void {
  const serialized = JSON.stringify(event);
  for (const value of forbiddenValues) {
    if (value && serialized.includes(value)) {
      throw new Error(
        `Instrumentation leaked forbidden value: ${value.slice(0, 40)}`
      );
    }
  }
}
