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

/**
 * The CDP-visible text for a key press. Enter must carry '\r': CDP fires the
 * default action (form submit, newline) only for text-producing keyDowns.
 */
export function browserKeyText(event: BrowserModifierEvent & {readonly key: string}): string {
  if (event.key === 'Enter' && !event.altKey && !event.ctrlKey && !event.metaKey) return '\r';
  return browserPrintableText(event);
}

/** Legacy Windows virtual-key codes for keys whose `key` is not a single char. */
const BROWSER_VIRTUAL_KEYS: Readonly<Record<string, number>> = {
  Backspace: 8,
  Tab: 9,
  Enter: 13,
  Shift: 16,
  Control: 17,
  Alt: 18,
  Pause: 19,
  CapsLock: 20,
  Escape: 27,
  PageUp: 33,
  PageDown: 34,
  End: 35,
  Home: 36,
  ArrowLeft: 37,
  ArrowUp: 38,
  ArrowRight: 39,
  ArrowDown: 40,
  Insert: 45,
  Delete: 46,
  Meta: 91,
  ContextMenu: 93,
  NumLock: 144,
  ScrollLock: 145,
};

/**
 * Virtual key code for a CDP key event. Without `windowsVirtualKeyCode` the
 * remote page sees `keyCode: 0` and no default action — sites listening for
 * keyCode 13 (Enter submits the search) or 8 (Backspace edits) never react.
 */
export function browserVirtualKeyCode(event: {readonly key: string; readonly keyCode?: number}): number {
  if (typeof event.keyCode === 'number' && Number.isInteger(event.keyCode) && event.keyCode > 0) {
    return event.keyCode;
  }
  const named = BROWSER_VIRTUAL_KEYS[event.key];
  if (named !== undefined) return named;
  if (event.key.length === 1) {
    const code = event.key.toUpperCase().charCodeAt(0);
    return code >= 32 && code < 127 ? code : 0;
  }
  const functionKey = /^F([1-9]|1\d|2[0-4])$/.exec(event.key);
  if (functionKey) return 111 + Number(functionKey[1]);
  return 0;
}
