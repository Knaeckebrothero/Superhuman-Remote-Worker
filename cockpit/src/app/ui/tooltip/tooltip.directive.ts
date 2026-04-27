import {
  DOCUMENT,
  Directive,
  ElementRef,
  HostListener,
  Renderer2,
  inject,
  input,
} from '@angular/core';

export type TooltipPlacement = 'top' | 'bottom' | 'left' | 'right';

let nextTooltipId = 0;

@Directive({
  selector: '[appTooltip]',
  standalone: true,
})
export class AppTooltipDirective {
  appTooltip = input<string>('');
  tooltipPlacement = input<TooltipPlacement>('top');
  tooltipDelay = input<number>(500);

  private host = inject(ElementRef<HTMLElement>);
  private renderer = inject(Renderer2);
  private doc = inject(DOCUMENT);

  private tooltipEl: HTMLElement | null = null;
  private showTimer: ReturnType<typeof setTimeout> | null = null;
  private readonly id = `app-tooltip-${++nextTooltipId}`;
  private readonly onScrollHide = () => this.hide();

  @HostListener('mouseenter')
  @HostListener('focusin')
  protected onEnter(): void {
    this.scheduleShow();
  }

  @HostListener('mouseleave')
  @HostListener('focusout')
  @HostListener('click')
  protected onLeave(): void {
    this.cancelShow();
    this.hide();
  }

  @HostListener('window:resize')
  protected onReposition(): void {
    if (this.tooltipEl) this.position();
  }

  @HostListener('document:keydown.escape')
  protected onEscape(): void {
    this.cancelShow();
    this.hide();
  }

  ngOnDestroy(): void {
    this.cancelShow();
    this.hide();
  }

  private scheduleShow(): void {
    if (!this.appTooltip() || this.tooltipEl) return;
    this.cancelShow();
    this.showTimer = setTimeout(() => this.show(), this.tooltipDelay());
  }

  private cancelShow(): void {
    if (this.showTimer != null) {
      clearTimeout(this.showTimer);
      this.showTimer = null;
    }
  }

  private show(): void {
    if (this.tooltipEl) return;
    const text = this.appTooltip();
    if (!text) return;

    const el = this.renderer.createElement('div') as HTMLElement;
    el.id = this.id;
    el.className = 'app-tooltip';
    el.setAttribute('role', 'tooltip');
    el.setAttribute('data-placement', this.tooltipPlacement());
    el.textContent = text;
    this.renderer.appendChild(this.doc.body, el);
    this.tooltipEl = el;
    this.renderer.setAttribute(this.host.nativeElement, 'aria-describedby', this.id);
    this.position();
    // Hide on any scroll within the page (capture catches inner scrollables).
    this.doc.addEventListener('scroll', this.onScrollHide, true);
  }

  private hide(): void {
    if (!this.tooltipEl) return;
    this.doc.removeEventListener('scroll', this.onScrollHide, true);
    this.renderer.removeChild(this.doc.body, this.tooltipEl);
    this.tooltipEl = null;
    this.renderer.removeAttribute(this.host.nativeElement, 'aria-describedby');
  }

  private position(): void {
    if (!this.tooltipEl) return;
    const hostRect = this.host.nativeElement.getBoundingClientRect();
    const tipRect = this.tooltipEl.getBoundingClientRect();
    const gap = 6;
    let top = 0;
    let left = 0;

    switch (this.tooltipPlacement()) {
      case 'top':
        top = hostRect.top - tipRect.height - gap;
        left = hostRect.left + (hostRect.width - tipRect.width) / 2;
        break;
      case 'bottom':
        top = hostRect.bottom + gap;
        left = hostRect.left + (hostRect.width - tipRect.width) / 2;
        break;
      case 'left':
        top = hostRect.top + (hostRect.height - tipRect.height) / 2;
        left = hostRect.left - tipRect.width - gap;
        break;
      case 'right':
        top = hostRect.top + (hostRect.height - tipRect.height) / 2;
        left = hostRect.right + gap;
        break;
    }

    // Clamp to viewport.
    const margin = 4;
    const maxLeft = this.doc.documentElement.clientWidth - tipRect.width - margin;
    const maxTop = this.doc.documentElement.clientHeight - tipRect.height - margin;
    left = Math.max(margin, Math.min(left, maxLeft));
    top = Math.max(margin, Math.min(top, maxTop));

    this.renderer.setStyle(this.tooltipEl, 'top', `${top}px`);
    this.renderer.setStyle(this.tooltipEl, 'left', `${left}px`);
  }
}
