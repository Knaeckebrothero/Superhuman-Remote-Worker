/**
 * Compact token-count / context-window label.
 *
 * Context windows are quoted in two conventions; this renders a clean label
 * for each. Exact decimal (128000 → "128k", 1_050_000 → "1.05M") and exact
 * binary / ×1024 (131072 → "128k", 262144 → "256k", 1_048_576 → "1M"). Falls
 * back to decimal-rounded ("123k") for anything irregular; sub-1000 as-is.
 *
 * Shared by the Admin → Models "Context" column + preset combobox and the
 * agent-settings model picker so every surface formats windows identically.
 */
export function formatTokens(n: number): string {
  if (n >= 1_000_000) {
    if (n % 1_048_576 === 0) return n / 1_048_576 + 'M'; // exact binary (1_048_576 → 1M)
    const m = n / 1_000_000; // decimal millions (1M, 1.05M)
    return (Number.isInteger(m) ? String(m) : m.toFixed(2).replace(/\.?0+$/, '')) + 'M';
  }
  if (n >= 1000) {
    if (n % 1000 === 0) return n / 1000 + 'k'; // exact decimal (128000 → 128k)
    if (n % 1024 === 0) return n / 1024 + 'k'; // exact binary (131072 → 128k)
    return Math.round(n / 1000) + 'k'; // irregular → decimal-rounded
  }
  return String(n);
}

/**
 * Parse a context-window entry into an integer token count. Accepts the
 * compact forms the preset combobox offers — "128k"/"1M" are ×1024 (binary),
 * matching how context windows are actually sized (128k = 131072) — plus a raw
 * number typed directly. Returns null for empty/invalid input.
 */
export function parseTokens(text: string): number | null {
  const t = text.trim().toLowerCase();
  if (!t) return null;
  const m = t.match(/^([\d.]+)\s*([km]?)$/);
  if (!m) return null;
  const num = parseFloat(m[1]);
  if (!Number.isFinite(num)) return null;
  if (m[2] === 'm') return Math.round(num * 1024 * 1024);
  if (m[2] === 'k') return Math.round(num * 1024);
  return Math.round(num);
}
