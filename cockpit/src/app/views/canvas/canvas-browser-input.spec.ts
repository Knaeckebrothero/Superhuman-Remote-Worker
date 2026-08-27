import { describe, expect, it } from 'vitest';
import {
  browserKeyText,
  browserModifiers,
  browserMouseButton,
  browserPrintableText,
  browserVirtualKeyCode,
  browserWheelDeltas,
  mapBrowserPoint,
} from './canvas-browser-input';

function rect(left: number, top: number, width: number, height: number): DOMRectReadOnly {
  return {left, top, width, height} as DOMRectReadOnly;
}

describe('shared-browser input geometry', () => {
  it.each([
    [50, 25, rect(0, 0, 100, 50), {width: 100, height: 50}, {x: 50, y: 25}],
    [110, 70, rect(10, 20, 200, 100), {width: 100, height: 50}, {x: 50, y: 25}],
    [60, 45, rect(10, 20, 100, 50), {width: 200, height: 100}, {x: 100, y: 50}],
    [41.25, 63.75, rect(1.25, 3.75, 80, 120), {width: 400, height: 900}, {x: 200, y: 450}],
    [125, 250, rect(25, 50, 200, 400), {width: 600, height: 1200}, {x: 300, y: 600}],
  ] as const)(
    'maps a displayed point independently of scale and orientation',
    (clientX, clientY, bounds, viewport, expected) => {
      expect(mapBrowserPoint(clientX, clientY, bounds, viewport)).toEqual(expected);
    },
  );

  it('uses STATE viewport metadata rather than decoded bitmap dimensions', () => {
    const bitmap = {width: 640, height: 480};
    expect(bitmap).not.toEqual({width: 1280, height: 720});
    expect(mapBrowserPoint(320, 180, rect(0, 0, 640, 360), {
      width: 1280,
      height: 720,
    })).toEqual({x: 640, y: 360});
  });

  it('accepts edges but clamps the right and bottom below viewport bounds', () => {
    const mapped = mapBrowserPoint(110, 70, rect(10, 20, 100, 50), {
      width: 1280,
      height: 720,
    });
    expect(mapped).not.toBeNull();
    expect(mapped!.x).toBeLessThan(1280);
    expect(mapped!.y).toBeLessThan(720);
    expect(mapped!.x).toBeGreaterThan(1279);
    expect(mapped!.y).toBeGreaterThan(719);
  });

  it.each([
    [Number.NaN, 0, rect(0, 0, 100, 100)],
    [0, 0, rect(0, 0, 0, 100)],
    [0, 0, rect(0, 0, 100, Number.POSITIVE_INFINITY)],
    [-0.01, 50, rect(0, 0, 100, 100)],
    [100.01, 50, rect(0, 0, 100, 100)],
    [50, 100.01, rect(0, 0, 100, 100)],
  ] as const)('rejects invalid geometry or an outside point', (x, y, bounds) => {
    expect(mapBrowserPoint(x, y, bounds, {width: 100, height: 100})).toBeNull();
  });

  it('encodes modifier and supported pointer-button values exactly', () => {
    expect(browserModifiers({altKey: true, ctrlKey: true, metaKey: true, shiftKey: true})).toBe(15);
    expect(browserModifiers({altKey: false, ctrlKey: true, metaKey: false, shiftKey: true})).toBe(10);
    expect([0, 1, 2, 3].map(browserMouseButton)).toEqual(['left', 'middle', 'right', null]);
  });

  it('normalizes pixel, line, and page wheel deltas into viewport pixels', () => {
    const bounds = rect(0, 0, 640, 360);
    const viewport = {width: 1280, height: 720};
    expect(browserWheelDeltas(5, -10, 0, bounds, viewport)).toEqual({
      deltaX: 10,
      deltaY: -20,
    });
    expect(browserWheelDeltas(1, -2, 1, bounds, viewport)).toEqual({
      deltaX: 32,
      deltaY: -64,
    });
    expect(browserWheelDeltas(1, -1, 2, bounds, viewport)).toEqual({
      deltaX: 1280,
      deltaY: -720,
    });
  });

  it('emits printable text only outside command chords', () => {
    const base = {altKey: false, ctrlKey: false, metaKey: false, shiftKey: true};
    expect(browserPrintableText({...base, key: 'A'})).toBe('A');
    expect(browserPrintableText({...base, key: 'Enter'})).toBe('');
    expect(browserPrintableText({...base, key: 'a', ctrlKey: true})).toBe('');
  });

  it('carries CR text for Enter so CDP fires the default action', () => {
    const base = {altKey: false, ctrlKey: false, metaKey: false, shiftKey: false};
    expect(browserKeyText({...base, key: 'Enter'})).toBe('\r');
    expect(browserKeyText({...base, key: 'Enter', ctrlKey: true})).toBe('');
    expect(browserKeyText({...base, key: 'A'})).toBe('A');
    expect(browserKeyText({...base, key: 'Escape'})).toBe('');
  });

  it('derives virtual key codes for named, printable, and function keys', () => {
    expect(browserVirtualKeyCode({key: 'Enter'})).toBe(13);
    expect(browserVirtualKeyCode({key: 'Backspace'})).toBe(8);
    expect(browserVirtualKeyCode({key: 'ArrowDown'})).toBe(40);
    expect(browserVirtualKeyCode({key: 'a'})).toBe(65);
    expect(browserVirtualKeyCode({key: '5'})).toBe(53);
    expect(browserVirtualKeyCode({key: 'F5'})).toBe(116);
    expect(browserVirtualKeyCode({key: 'F12'})).toBe(123);
    // A real browser event's own keyCode wins over the fallback table.
    expect(browserVirtualKeyCode({key: 'Enter', keyCode: 13})).toBe(13);
    expect(browserVirtualKeyCode({key: 'ü'})).toBe(0);
    expect(browserVirtualKeyCode({key: 'MediaPlayPause'})).toBe(0);
  });
});
