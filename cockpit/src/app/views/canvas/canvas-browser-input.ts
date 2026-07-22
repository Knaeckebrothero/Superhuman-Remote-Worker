export interface BrowserViewport {
  readonly width: number;
  readonly height: number;
}

export interface BrowserPoint {
  readonly x: number;
  readonly y: number;
}

export interface BrowserModifierEvent {
  readonly altKey: boolean;
  readonly ctrlKey: boolean;
  readonly metaKey: boolean;
  readonly shiftKey: boolean;
}

export type BrowserMouseButton = 'left' | 'middle' | 'right';

/** Map the exact displayed canvas rectangle into CDP CSS viewport pixels. */
export function mapBrowserPoint(
  clientX: number,
  clientY: number,
  rect: DOMRectReadOnly,
  viewport: BrowserViewport,
): BrowserPoint | null {
  if (
    ![clientX, clientY, rect.left, rect.top, rect.width, rect.height].every(Number.isFinite) ||
    !Number.isFinite(viewport.width) ||
    !Number.isFinite(viewport.height) ||
    rect.width <= 0 ||
    rect.height <= 0 ||
    viewport.width <= 0 ||
    viewport.height <= 0
  ) {
    return null;
  }
  const displayX = clientX - rect.left;
  const displayY = clientY - rect.top;
  if (displayX < 0 || displayY < 0 || displayX > rect.width || displayY > rect.height) {
    return null;
  }

  const maxX = viewport.width - Number.EPSILON * Math.max(1, viewport.width);
  const maxY = viewport.height - Number.EPSILON * Math.max(1, viewport.height);
  return {
    x: Math.min(maxX, displayX * viewport.width / rect.width),
    y: Math.min(maxY, displayY * viewport.height / rect.height),
  };
}

/** CDP modifier flags: Alt=1, Ctrl=2, Meta=4, Shift=8. */
export function browserModifiers(event: BrowserModifierEvent): number {
  return (event.altKey ? 1 : 0) |
    (event.ctrlKey ? 2 : 0) |
    (event.metaKey ? 4 : 0) |
    (event.shiftKey ? 8 : 0);
}

export function browserMouseButton(button: number): BrowserMouseButton | null {
  if (button === 0) return 'left';
  if (button === 1) return 'middle';
  if (button === 2) return 'right';
  return null;
}

/** Normalize DOM wheel units and scale displayed CSS deltas into viewport CSS pixels. */
export function browserWheelDeltas(
  deltaX: number,
  deltaY: number,
  deltaMode: number,
  rect: DOMRectReadOnly,
  viewport: BrowserViewport,
): {deltaX: number; deltaY: number} | null {
  if (
    ![deltaX, deltaY, deltaMode, rect.width, rect.height].every(Number.isFinite) ||
    rect.width <= 0 ||
    rect.height <= 0 ||
    !Number.isFinite(viewport.width) ||
    !Number.isFinite(viewport.height) ||
    viewport.width <= 0 ||
    viewport.height <= 0 ||
    ![0, 1, 2].includes(deltaMode)
  ) {
    return null;
  }
  const cssDeltaX = deltaMode === 1
    ? deltaX * 16
    : deltaMode === 2
      ? deltaX * rect.width
      : deltaX;
  const cssDeltaY = deltaMode === 1
    ? deltaY * 16
    : deltaMode === 2
      ? deltaY * rect.height
      : deltaY;
  return {
    deltaX: cssDeltaX * viewport.width / rect.width,
    deltaY: cssDeltaY * viewport.height / rect.height,
  };
}

export function browserPrintableText(event: BrowserModifierEvent & {readonly key: string}): string {
  return event.key.length === 1 && !event.altKey && !event.ctrlKey && !event.metaKey
    ? event.key
    : '';
}
