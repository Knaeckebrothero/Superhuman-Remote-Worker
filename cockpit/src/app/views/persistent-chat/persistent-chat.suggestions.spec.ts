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

import {readFileSync} from 'node:fs';

interface SuggestionEntry {
  icon: string;
  en: string;
  de: string;
}

const suggestions: SuggestionEntry[] = JSON.parse(
  readFileSync('src/assets/suggestions.json', 'utf8'),
);

describe('suggestions.json', () => {
  it('holds exactly the four chips that are displayed, so none can vanish', () => {
    expect(suggestions).toHaveLength(4);
  });

  it('every entry has an icon and both languages', () => {
    for (const s of suggestions) {
      expect(s.icon, `icon missing on ${JSON.stringify(s)}`).toBeTruthy();
      expect(s.en, `en missing on ${JSON.stringify(s)}`).toBeTruthy();
      expect(s.de, `de missing on ${JSON.stringify(s)}`).toBeTruthy();
    }
  });

  it('keeps chip text within the length the layout lane can fit, in both languages', () => {
    // German runs 20-55% longer than English here and is what the layout lane
    // measures (Ruling T34-1). 45 is an empirical tripwire, not a proof: the real
    // check is `npm run test:e2e:empty-state`, which asserts zero overflow at
    // 412x915. This exists so a too-long string fails in seconds with a clear
    // message instead of reddening the slow lane.
    const MAX_CHIP_CHARS = 45;
    for (const s of suggestions) {
      expect(s.en.length, `en too long: "${s.en}"`).toBeLessThanOrEqual(MAX_CHIP_CHARS);
      expect(s.de.length, `de too long: "${s.de}"`).toBeLessThanOrEqual(MAX_CHIP_CHARS);
    }
  });

  it('never lets a retired phrase back in, in either language', () => {
    const RETIRED = ["project i've shared", 'surprise me', 'geteilten projekt', 'überrasche mich'];
    for (const s of suggestions) {
      for (const phrase of RETIRED) {
        expect(`${s.en} ${s.de}`.toLowerCase(), `retired phrase "${phrase}" is back`).not.toContain(phrase);
      }
    }
  });
});
