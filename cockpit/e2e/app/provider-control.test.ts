import { describe, expect, it } from 'vitest';
import {
  expectedReplyWasConsumed,
  requireCleanProviderState,
  type ProviderCounter,
  type ProviderScenarioState,
} from './provider-control';

const RUN_ID = '2f162ad8-5792-409f-bc4b-704099bd07c6';
const CHAT_MODEL = 'e2e-chat';

function expectedCounter(overrides: Partial<ProviderCounter> = {}): ProviderCounter {
  return {
    run_id: RUN_ID,
    model: CHAT_MODEL,
    endpoint: 'chat.completions',
    stream: true,
    outcome: 'success',
    count: 1,
    ...overrides,
  };
}

function consumedState(overrides: Partial<ProviderScenarioState> = {}): ProviderScenarioState {
  return {
    run_id: RUN_ID,
    scenario: 'reply',
    required_responses: 1,
    consumed_required_responses: 1,
    remaining_required_responses: 0,
    unexpected_count: 0,
    pending_calls: 0,
    counters: [expectedCounter()],
    calls: [],
    ...overrides,
  };
}

describe('expectedReplyWasConsumed', () => {
  it('accepts exactly one required streamed success', () => {
    expect(expectedReplyWasConsumed(consumedState(), RUN_ID, CHAT_MODEL)).toBe(true);
  });

  it.each([
    ['a different scenario', { scenario: 'numbered-stream' }],
    ['more than one required response', { required_responses: 2 }],
    ['more than one consumed response', { consumed_required_responses: 2 }],
    ['a remaining required response', { remaining_required_responses: 1 }],
  ] as const)('rejects %s', (_label, override) => {
    expect(expectedReplyWasConsumed(consumedState(override), RUN_ID, CHAT_MODEL)).toBe(false);
  });

  it('rejects an aggregated success count greater than one', () => {
    const state = consumedState({ counters: [expectedCounter({ count: 2 })] });
    expect(expectedReplyWasConsumed(state, RUN_ID, CHAT_MODEL)).toBe(false);
  });

  it('rejects duplicate matching success counters', () => {
    const state = consumedState({ counters: [expectedCounter(), expectedCounter()] });
    expect(expectedReplyWasConsumed(state, RUN_ID, CHAT_MODEL)).toBe(false);
  });

  it.each([
    ['wrong outcome', { outcome: 'provider_error' }],
    ['wrong endpoint', { endpoint: 'embeddings' }],
    ['wrong model', { model: 'not-the-chat-model' }],
    ['non-streaming response', { stream: false }],
    ['wrong run', { run_id: '904d1608-91cb-4e87-9918-dc305b1a1751' }],
  ] as const)('rejects a counter with the %s', (_label, counterOverride) => {
    const state = consumedState({ counters: [expectedCounter(counterOverride)] });
    expect(expectedReplyWasConsumed(state, RUN_ID, CHAT_MODEL)).toBe(false);
  });
});

describe('requireCleanProviderState', () => {
  it('does not add a missing-reply error after an earlier test-body failure', () => {
    const state = consumedState({
      consumed_required_responses: 0,
      remaining_required_responses: 1,
      counters: [],
    });
    const overview = { runs: [state], unscoped_unexpected_calls: 0 };

    expect(() =>
      requireCleanProviderState(
        state,
        RUN_ID,
        CHAT_MODEL,
        { requireIdle: true, requireReply: false },
        overview,
      ),
    ).not.toThrow();
    expect(() =>
      requireCleanProviderState(
        state,
        RUN_ID,
        CHAT_MODEL,
        { requireIdle: true, requireReply: true },
        overview,
      ),
    ).toThrow(/did not consume/);
  });
});
