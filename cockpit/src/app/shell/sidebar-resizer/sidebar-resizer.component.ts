import { Component, OnDestroy, inject } from '@angular/core';
import { TranslocoPipe } from '@jsverse/transloco';
import {
  SIDEBAR_MAX_WIDTH,
  SIDEBAR_MIN_WIDTH,
  SIDEBAR_WIDTH_STEP,
  SidebarService,
} from '../../core/services/sidebar.service';

/** Body class held for the duration of a drag — see styles.scss. */
const DRAG_CLASS = 'sidebar-resizing';

/**
 * The drag handle that sets the rail's width, rendered by the app shell in the
 * seam between the rail and the content area.
 *
 * It lives here rather than inside the rail for one concrete reason: the rail's
 * session list owns a thin scrollbar flush against its right edge, and a handle
 * placed inside the rail sits on top of that scrollbar and eats the thumb. As a
 * zero-width flex item *after* the rail, the handle's hit area straddles the
 * seam — mostly on the content side — and leaves the scrollbar grabbable.
 *
 * The interaction model is the one the assistant UIs and IDEs have converged
 * on: drag the edge, double-click to restore the default, width persisted per
 * device. The handle is also a real ARIA window splitter (a focusable
 * `separator` with a value), so the width is reachable without a pointer —
 * arrow keys nudge, Home/End jump to the bounds.
 */
@Component({
  selector: 'app-sidebar-resizer',
  standalone: true,
  imports: [TranslocoPipe],
  template: `
    <div
      class="handle"
      role="separator"
      tabindex="0"
      aria-orientation="vertical"
      aria-controls="sidebar-rail"
      [attr.aria-label]="'nav.resizeSidebar' | transloco"
      [attr.aria-valuenow]="sidebar.width()"
      [attr.aria-valuemin]="min"
      [attr.aria-valuemax]="max"
      (pointerdown)="onPointerDown($event)"
      (pointermove)="onPointerMove($event)"
      (pointerup)="onPointerUp($event)"
      (pointercancel)="onPointerUp($event)"
      (dblclick)="sidebar.resetWidth()"
      (keydown)="onKeydown($event)"
    ></div>
  `,
  styles: [
    `
      /* Zero-width flex item: the handle is an overlay on the seam, never a
         gutter. A real-width item would open a strip of app background between
         the rail's border and the content area's own surface — a visible seam
         at all times, in exchange for hit area we can get for free by
         overflowing. */
      :host {
        display: block;
        position: relative;
        width: 0;
        flex: none;
        z-index: 50;
      }

      /* Straddles the seam asymmetrically: 2px over the rail (its 1px border
         plus one), 6px over the content. The rail's session list scrolls under
         a thin scrollbar occupying roughly the 8px inside that border, so
         reaching further left would trade the scrollbar thumb for hit area. */
      .handle {
        position: absolute;
        top: 0;
        bottom: 0;
        left: -2px;
        width: 8px;
        background: transparent;
        cursor: col-resize;
        /* A pen/touch drag on a hybrid desktop must resize, not scroll the
           content area underneath. */
        touch-action: none;
      }

      /* The visible affordance: the rail's own 1px border appears to thicken
         and light up. Painted on ::after so the hit area can stay 8px wide
         without a 8px-wide accent bar. */
      .handle::after {
        content: '';
        position: absolute;
        top: 0;
        bottom: 0;
        left: 1px;
        width: 2px;
        background: transparent;
        transition: background 0.15s ease;
      }

      .handle:hover::after,
      .handle:focus-visible::after,
      :host(.dragging) .handle::after {
        background: var(--accent-color);
      }

      /* The ::after line above *is* the focus indicator — an outline on an
         8px-wide invisible strip renders as a floating box next to the rail. */
      .handle:focus {
        outline: none;
      }

      /* Touch devices get the drawer, not a draggable rail; a hover-revealed
         2px line is also not a target a finger can find. The shell already
         withholds this component below 768px — this is the belt to that
         suspenders for hybrid devices that report no hover. */
      @media (hover: none) {
        :host {
          display: none;
        }
      }
    `,
  ],
  host: {
    '[class.dragging]': 'sidebar.resizing()',
  },
})
export class SidebarResizerComponent implements OnDestroy {
  readonly sidebar = inject(SidebarService);

  readonly min = SIDEBAR_MIN_WIDTH;
  readonly max = SIDEBAR_MAX_WIDTH;

  /** Pointer x and rail width at gesture start. The delta is measured against
   *  these rather than read from clientX directly, so the handle doesn't jump
   *  to centre itself under the pointer on the first move, and so the maths
   *  survives a rail that isn't flush against the viewport edge. */
  private startX = 0;
  private startWidth = 0;

  onPointerDown(event: PointerEvent): void {
    // Primary button only: a right-click drag isn't a resize, and a
    // context-menu-interrupted gesture would leave the body class set.
    if (event.button !== 0) return;
    const el = event.target as HTMLElement;
    // Capture keeps move/up on this element once the pointer leaves the 8px
    // strip — which it does immediately on any real drag.
    el.setPointerCapture?.(event.pointerId);
    this.startX = event.clientX;
    this.startWidth = this.sidebar.width();
    this.sidebar.resizing.set(true);
    document.body.classList.add(DRAG_CLASS);
    // Suppresses the compatibility mousedown, and with it the text selection
    // that a drag across the transcript would otherwise start. That also drops
    // the focus mousedown would have given the handle, so take it explicitly —
    // :focus-visible won't match a pointer focus, so no ring appears.
    event.preventDefault();
    el.focus?.();
  }

  onPointerMove(event: PointerEvent): void {
    if (!this.sidebar.resizing()) return;
    this.sidebar.setWidth(this.startWidth + (event.clientX - this.startX));
  }

  onPointerUp(event: PointerEvent): void {
    if (!this.sidebar.resizing()) return;
    const el = event.target as HTMLElement;
    if (el.hasPointerCapture?.(event.pointerId)) {
      el.releasePointerCapture(event.pointerId);
    }
    this.endDrag();
    // One write per gesture — setWidth() during the drag deliberately doesn't
    // touch localStorage.
    this.sidebar.commitWidth();
  }

  onKeydown(event: KeyboardEvent): void {
    let next: number;
    switch (event.key) {
      case 'ArrowLeft':
        next = this.sidebar.width() - SIDEBAR_WIDTH_STEP;
        break;
      case 'ArrowRight':
        next = this.sidebar.width() + SIDEBAR_WIDTH_STEP;
        break;
      case 'Home':
        next = SIDEBAR_MIN_WIDTH;
        break;
      case 'End':
        next = SIDEBAR_MAX_WIDTH;
        break;
      default:
        return;
    }
    // Only after a key we handle: arrow keys still scroll, and Home/End still
    // jump, when focus is anywhere else.
    event.preventDefault();
    this.sidebar.setWidth(next);
    this.sidebar.commitWidth();
  }

  /** The shell unmounts this component when the rail collapses or the viewport
   *  crosses into drawer territory — either can happen mid-gesture, and the
   *  body class outlives the component that set it. */
  ngOnDestroy(): void {
    if (this.sidebar.resizing()) this.endDrag();
  }

  private endDrag(): void {
    this.sidebar.resizing.set(false);
    document.body.classList.remove(DRAG_CLASS);
  }
}
