import {beforeEach, describe, expect, it, vi} from 'vitest';
import {
  PersistentChatComponent,
  draftKey,
  loadDraft,
} from './persistent-chat.component';

/** The only fields pickSuggestion touches. */
function fakeThis(threadId: string | null) {
  return {
    inputText: '',
    chat: {threadId: () => threadId},
    inputEl: undefined,
    autoResizeInput: vi.fn(),
  };
}

function pick(ctx: ReturnType<typeof fakeThis>, text: string) {
  PersistentChatComponent.prototype.pickSuggestion.call(ctx, {icon: 'route', text});
}

describe('pickSuggestion', () => {
  beforeEach(() => {
    sessionStorage.clear();
    vi.useFakeTimers(); // the method schedules a focus() via setTimeout
  });

  it('fills the composer with the chip text', () => {
    const ctx = fakeThis('thread-1');
    pick(ctx, 'Review the k8s manifest in srw-cloud');
    expect(ctx.inputText).toBe('Review the k8s manifest in srw-cloud');
  });

  it('persists the draft, so the text survives a reload', () => {
    const ctx = fakeThis('thread-1');
    pick(ctx, 'Review the k8s manifest in srw-cloud');
    // This is the assertion that fails before the fix: the assignment fires no
    // ngModelChange, so nothing wrote the draft.
    expect(loadDraft('thread-1')).toBe('Review the k8s manifest in srw-cloud');
    expect(sessionStorage.getItem(draftKey('thread-1'))).toBe(
      'Review the k8s manifest in srw-cloud',
    );
  });

  it('does not throw on the draft landing, where there is no thread id yet', () => {
    const ctx = fakeThis(null);
    expect(() => pick(ctx, 'Summarize this week')).not.toThrow();
    expect(ctx.inputText).toBe('Summarize this week');
  });
});
