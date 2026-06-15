import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {copyText} from './copy-text';

/**
 * `copyText` is the clipboard primitive behind <app-copy-field>. It must prefer
 * the async Clipboard API and degrade to a `document.execCommand('copy')`
 * textarea fallback when that API is missing or rejects (e.g. insecure context
 * or denied permission).
 */
describe('copyText', () => {
  let originalClipboard: PropertyDescriptor | undefined;
  let originalExecCommand: typeof document.execCommand;

  beforeEach(() => {
    originalClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard');
    originalExecCommand = document.execCommand;
  });

  afterEach(() => {
    if (originalClipboard) {
      Object.defineProperty(navigator, 'clipboard', originalClipboard);
    } else {
      delete (navigator as {clipboard?: unknown}).clipboard;
    }
    document.execCommand = originalExecCommand;
    vi.restoreAllMocks();
  });

  function setClipboard(value: unknown): void {
    Object.defineProperty(navigator, 'clipboard', {
      value,
      configurable: true,
      writable: true,
    });
  }

  it('writes the text via the async Clipboard API and returns true', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    setClipboard({writeText});

    const ok = await copyText('job-5cc78c52-full');

    expect(ok).toBe(true);
    expect(writeText).toHaveBeenCalledWith('job-5cc78c52-full');
  });

  it('falls back to execCommand when the Clipboard API is unavailable', async () => {
    setClipboard(undefined);
    const execCommand = vi.fn().mockReturnValue(true);
    document.execCommand = execCommand;

    const ok = await copyText('job-5cc78c52-full');

    expect(ok).toBe(true);
    expect(execCommand).toHaveBeenCalledWith('copy');
  });

  it('falls back to execCommand when the Clipboard API rejects', async () => {
    setClipboard({writeText: vi.fn().mockRejectedValue(new Error('denied'))});
    const execCommand = vi.fn().mockReturnValue(true);
    document.execCommand = execCommand;

    const ok = await copyText('job-5cc78c52-full');

    expect(ok).toBe(true);
    expect(execCommand).toHaveBeenCalledWith('copy');
  });

  it('returns false when both the Clipboard API and the fallback fail', async () => {
    setClipboard({writeText: vi.fn().mockRejectedValue(new Error('denied'))});
    document.execCommand = vi.fn(() => {
      throw new Error('execCommand unavailable');
    });

    const ok = await copyText('job-5cc78c52-full');

    expect(ok).toBe(false);
  });
});
