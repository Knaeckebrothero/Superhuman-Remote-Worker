import type {ButtonVariant} from './button.component';

export type ButtonAppearance = 'subtle' | 'solid';

const TINTED: ReadonlySet<ButtonVariant> = new Set<ButtonVariant>(['success', 'warning', 'info', 'danger']);

/**
 * Tinted variants render subtle (tint background, tone text) unless a caller
 * asks for solid — the one confirm button in a destructive dialog. The three
 * structural variants (primary, secondary, ghost) have no appearance axis and
 * return null so no data-appearance attribute is written for them.
 */
export function resolveAppearance(
  variant: ButtonVariant,
  appearance: ButtonAppearance | undefined,
): ButtonAppearance | null {
  if (!TINTED.has(variant)) return null;
  return appearance ?? 'subtle';
}
