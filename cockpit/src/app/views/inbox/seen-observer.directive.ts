import {Directive, ElementRef, inject, input, OnDestroy, OnInit} from '@angular/core';
import {ActionCenterService} from '../../core/services/action-center.service';

/**
 * Stamps a feed row `seen` once it has actually been in front of the user
 * (unified notification system, D4: escalation gates on *seen*, not read).
 * Half the row visible for one intersection callback is enough; the service
 * batches and debounces the POST.
 */
@Directive({
  selector: '[appSeenObserver]',
  standalone: true,
})
export class SeenObserverDirective implements OnInit, OnDestroy {
  /** The notification id, or null for legacy items (no-op). */
  readonly notificationId = input<string | null>(null, {alias: 'appSeenObserver'});

  private readonly el = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly actionCenter = inject(ActionCenterService);
  private observer: IntersectionObserver | null = null;

  ngOnInit(): void {
    const id = this.notificationId();
    if (!id || typeof IntersectionObserver === 'undefined') return;
    this.observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          this.actionCenter.noteSeen(id);
          this.observer?.disconnect();
          this.observer = null;
        }
      },
      {threshold: 0.5},
    );
    this.observer.observe(this.el.nativeElement);
  }

  ngOnDestroy(): void {
    this.observer?.disconnect();
    this.observer = null;
  }
}
