/**
 * Client-side binary detection for the cloud diff viewer.
 *
 * Why this exists on top of the backend's flags: protected staging marks a
 * file binary only when its first 8 KiB contains a NUL byte
 * (protected_cloud_review.md PC-17). A valid PDF whose bytes happen to
 * decode as UTF-8 without a NUL is therefore reported `binary: false` in the
 * summary, and its raw syntax would be handed to Monaco as if it were text.
 * The per-file reader is stricter (it rejects non-UTF-8), but the review
 * surface must not depend on the summary flag for a safety decision — the
 * issue note is explicit that it "must not be treated as a review-safety
 * decision".
 *
 * So the surface sniffs the content it is about to render. Pure functions,
 * no I/O, so both are directly unit-testable.
 *
 * **What this does and does not guarantee.** It is a heuristic, biased
 * toward calling something binary: a false positive costs a placeholder
 * instead of a diff, a false negative paints raw file syntax into an editor.
 * It reliably catches a NUL byte, an upstream decode failure, the signatures
 * listed below and control-byte soup. It cannot catch a format whose
 * signature it does not know and whose head decodes as ordinary text — so
 * the honest claim is "no *detected* binary reaches the editor", not "no
 * binary can".
 *
 * Escapes are written as `String.fromCharCode` rather than string escapes on
 * purpose: this file has been corrupted twice by tooling that rewrote
 * backslash-u sequences into the literal control characters they denote,
 * which is invisible in a diff and changes what the sniff matches.
 */

/** Only the first slice matters — magic numbers and NULs cluster at the head,
 *  and scanning a 40 MB string to decide how to render it is wasteful. */
const SNIFF_WINDOW = 4096;

/** Ratio of C0 control characters above which text is treated as binary. */
const CONTROL_CHAR_RATIO = 0.01;

/**
 * How far into the head a signature may sit and still count as the header.
 *
 * ISO 32000 does not require `%PDF-` at byte 0: the header may be preceded by
 * junk, and conforming readers scan roughly the first kilobyte for it. Real
 * files pick up a BOM or a stray newline often enough that anchoring strictly
 * at index 0 loses the exact case this guard exists for.
 */
const SIGNATURE_SCAN_WINDOW = 1024;

const NUL = String.fromCharCode(0);
/** U+FFFD — the bytes already failed a UTF-8 decode somewhere upstream. */
const REPLACEMENT = String.fromCharCode(0xfffd);
const BOM = 0xfeff;

/**
 * File signatures that survive a lossy decode. `%PDF-` is the important one:
 * it is exactly the case the NUL heuristic misses. The others are cheap
 * insurance for formats whose headers are ASCII-ish.
 */
const MAGIC_PREFIXES = [
  '%PDF-', // PDF
  'PK' + String.fromCharCode(3, 4), // ZIP family: docx, xlsx, pptx, odt, jar
  'PK' + String.fromCharCode(5, 6), // empty ZIP
  String.fromCharCode(0x7f) + 'ELF', // ELF executable
  'GIF8', // GIF
  String.fromCharCode(0xff, 0xd8, 0xff), // JPEG, latin-1-decoded
  'ID3', // MP3 with an ID3 tag
  '%!PS', // PostScript
];

/**
 * Whether every character is padding a file may legitimately carry ahead of
 * its signature: whitespace, C0 controls, or a byte-order mark.
 *
 * This is what keeps prose that merely *mentions* `%PDF-` out of the binary
 * bucket — a sentence before the match is not padding, so the match is not a
 * header.
 */
function isPreHeaderPadding(text: string): boolean {
  for (let i = 0; i < text.length; i++) {
    const code = text.charCodeAt(i);
    if (code > 0x20 && code !== BOM) return false;
  }
  return true;
}

/** Whether `head` carries one of the known signatures as its actual header. */
function hasFileSignature(head: string): boolean {
  const window =
    head.length > SIGNATURE_SCAN_WINDOW ? head.slice(0, SIGNATURE_SCAN_WINDOW) : head;
  for (const magic of MAGIC_PREFIXES) {
    const at = window.indexOf(magic);
    if (at === 0) return true;
    if (at > 0 && isPreHeaderPadding(window.slice(0, at))) return true;
  }
  return false;
}

/**
 * Whether a decoded string should be treated as binary rather than fed to a
 * text diff editor.
 *
 * `null`/empty is NOT binary: an added file has a null `old_content` and a
 * deleted file has a null `new_content`, and neither says anything about the
 * side that does exist.
 */
export function looksBinaryContent(content: string | null | undefined): boolean {
  if (!content) return false;
  const head = content.length > SNIFF_WINDOW ? content.slice(0, SNIFF_WINDOW) : content;

  if (head.includes(NUL)) return true;
  if (head.includes(REPLACEMENT)) return true;
  if (hasFileSignature(head)) return true;

  // C0 controls other than tab/newline/carriage-return do not occur in
  // real-world text files in any quantity. A form feed or an escape here and
  // there is fine (some generated files carry them), so this is a ratio, not
  // a tripwire.
  let controls = 0;
  for (let i = 0; i < head.length; i++) {
    const code = head.charCodeAt(i);
    if (code < 0x20 && code !== 0x09 && code !== 0x0a && code !== 0x0d) controls++;
  }
  return controls / head.length > CONTROL_CHAR_RATIO;
}

/**
 * Whether the selected entry must render the binary placeholder instead of
 * the diff editor. Three independent signals, OR'd, cheapest first:
 *
 * 1. the summary's per-file flag (thread mode only; unreliable per PC-17),
 * 2. the per-file reader's `old_binary`/`new_binary` (reliable; job mode's
 *    `JobDiffFile` has neither field, so it contributes nothing there),
 * 3. a content sniff of whichever sides actually arrived (this module).
 *
 * The third is what stops a UTF-8-decodable PDF from reaching Monaco in
 * either mode — job mode has no binary flags at all, so before this it had
 * no protection whatsoever.
 */
export function isBinaryEntry(
  sum: { binary?: boolean },
  file: {
    old_binary?: boolean;
    new_binary?: boolean;
    old_content?: string | null;
    new_content?: string | null;
  } | null,
): boolean {
  if (sum.binary || file?.old_binary || file?.new_binary) return true;
  if (!file) return false;
  return looksBinaryContent(file.old_content) || looksBinaryContent(file.new_content);
}

/**
 * Coarse content kind from the path extension, for the placeholder's "what
 * is this" line. Returns an i18n leaf under
 * `jobDiffReview.viewer.binaryKind.*` — deliberately a small closed set,
 * because the placeholder is a safety statement, not a file manager.
 *
 * Only ever consulted once something else has already decided the entry is
 * binary, so text-ish members of these families (`.csv`, `.svg`) being listed
 * here cannot force a text file into the placeholder.
 */
export function binaryKindFromPath(path: string): string {
  const ext = path.split('.').pop()?.toLowerCase() ?? '';
  if (ext === 'pdf') return 'pdf';
  if (['doc', 'docx', 'odt', 'rtf'].includes(ext)) return 'document';
  if (['xls', 'xlsx', 'ods', 'csv'].includes(ext)) return 'spreadsheet';
  if (['ppt', 'pptx', 'odp'].includes(ext)) return 'presentation';
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp', 'tiff', 'heic'].includes(ext)) {
    return 'image';
  }
  if (['mp3', 'wav', 'flac', 'ogg', 'm4a', 'aac'].includes(ext)) return 'audio';
  if (['mp4', 'mov', 'mkv', 'webm', 'avi'].includes(ext)) return 'video';
  if (['zip', 'tar', 'gz', 'bz2', 'xz', '7z', 'rar'].includes(ext)) return 'archive';
  return 'unknown';
}
