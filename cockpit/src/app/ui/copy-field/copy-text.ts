/**
 * Copy `text` to the clipboard, returning whether it succeeded.
 *
 * Prefers the async Clipboard API and degrades to a hidden-textarea
 * `document.execCommand('copy')` fallback when that API is missing (insecure
 * context / older browsers) or rejects (denied permission).
 */
export async function copyText(text: string): Promise<boolean> {
  const clipboard = typeof navigator !== 'undefined' ? navigator.clipboard : undefined;
  if (clipboard?.writeText) {
    try {
      await clipboard.writeText(text);
      return true;
    } catch {
      // Fall through to the execCommand fallback below.
    }
  }

  try {
    const textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.style.position = 'fixed';
    textarea.style.opacity = '0';
    document.body.appendChild(textarea);
    textarea.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(textarea);
    return ok;
  } catch {
    return false;
  }
}
