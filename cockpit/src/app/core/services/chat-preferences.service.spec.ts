import {beforeEach, describe, expect, it} from 'vitest';
import {TestBed} from '@angular/core/testing';
import {ChatPreferencesService} from './chat-preferences.service';

const KEY = 'cockpit:chat:reasoningExpanded';
const TOOL_KEY = 'cockpit:chat:toolCallsExpanded';

describe('ChatPreferencesService', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    window.localStorage.clear();
  });

  it('defaults reasoningExpanded to true when nothing is stored', () => {
    const svc = TestBed.inject(ChatPreferencesService);
    expect(svc.reasoningExpanded()).toBe(true);
  });

  it('reads a stored "false" preference at construction', () => {
    window.localStorage.setItem(KEY, 'false');
    const svc = TestBed.inject(ChatPreferencesService);
    expect(svc.reasoningExpanded()).toBe(false);
  });

  it('reads a stored "true" preference at construction', () => {
    window.localStorage.setItem(KEY, 'true');
    const svc = TestBed.inject(ChatPreferencesService);
    expect(svc.reasoningExpanded()).toBe(true);
  });

  it('falls back to the default (true) for a malformed stored value', () => {
    window.localStorage.setItem(KEY, 'garbage');
    const svc = TestBed.inject(ChatPreferencesService);
    expect(svc.reasoningExpanded()).toBe(true);
  });

  it('updates the signal and persists on set', () => {
    const svc = TestBed.inject(ChatPreferencesService);

    svc.setReasoningExpanded(false);
    expect(svc.reasoningExpanded()).toBe(false);
    expect(window.localStorage.getItem(KEY)).toBe('false');

    svc.setReasoningExpanded(true);
    expect(svc.reasoningExpanded()).toBe(true);
    expect(window.localStorage.getItem(KEY)).toBe('true');
  });

  it('round-trips a persisted value into a fresh service instance', () => {
    TestBed.inject(ChatPreferencesService).setReasoningExpanded(false);
    TestBed.resetTestingModule(); // new injector → new service, same localStorage
    expect(TestBed.inject(ChatPreferencesService).reasoningExpanded()).toBe(false);
  });

  it('defaults toolCallsExpanded to false (folded) when nothing is stored', () => {
    expect(TestBed.inject(ChatPreferencesService).toolCallsExpanded()).toBe(false);
  });

  it('reads a stored "true" toolCallsExpanded preference', () => {
    window.localStorage.setItem(TOOL_KEY, 'true');
    expect(TestBed.inject(ChatPreferencesService).toolCallsExpanded()).toBe(true);
  });

  it('persists toolCallsExpanded independently of reasoningExpanded', () => {
    const svc = TestBed.inject(ChatPreferencesService);
    svc.setToolCallsExpanded(true);
    expect(svc.toolCallsExpanded()).toBe(true);
    expect(window.localStorage.getItem(TOOL_KEY)).toBe('true');
    // reasoning untouched (still its default)
    expect(svc.reasoningExpanded()).toBe(true);
    expect(window.localStorage.getItem(KEY)).toBeNull();
  });
});
