import {ChangeDetectionStrategy, Component, DestroyRef, OnInit, inject} from '@angular/core';
import {takeUntilDestroyed} from '@angular/core/rxjs-interop';
import {ActivatedRoute, Router} from '@angular/router';
import {distinctUntilChanged, map} from 'rxjs';
import {CanvasService} from '../../core/services/canvas.service';
import {CanvasPaneComponent} from './canvas-pane.component';

export const CANVAS_POPOUT_RECONCILE_MS = 15_000;

/** Authenticated full-window wrapper; untrusted apps remain sandboxed in CanvasPane. */
@Component({
  selector: 'app-canvas-popout-page',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CanvasPaneComponent],
  template: `
    <main class="canvas-popout">
      <app-canvas-pane [active]="true" [popout]="true"
                       (closeRequested)="closePopOut()" />
    </main>
  `,
  styles: `
    :host,
    .canvas-popout,
    app-canvas-pane {
      display: block;
      width: 100%;
      height: 100%;
      min-width: 0;
    }

    :host { overflow: hidden; }
  `,
})
export class CanvasPopoutPageComponent implements OnInit {
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly canvas = inject(CanvasService);
  private readonly destroyRef = inject(DestroyRef);
  private reconcileTimer: number | null = null;

  ngOnInit(): void {
    this.route.paramMap.pipe(
      map(params => params.get('threadId')),
      distinctUntilChanged(),
      takeUntilDestroyed(this.destroyRef),
    ).subscribe(threadId => {
      this.canvas.selectThread(threadId);
      if (!threadId) void this.router.navigate(['/sessions']);
    });
    this.startReconcilePolling();
  }

  closePopOut(): void {
    if (typeof window !== 'undefined') {
      window.close();
      if (window.closed) return;
    }
    const threadId = this.canvas.threadId();
    void this.router.navigate(threadId ? ['/sessions', threadId] : ['/sessions']);
  }

  private startReconcilePolling(): void {
    if (typeof window === 'undefined' || typeof document === 'undefined') return;
    this.reconcileTimer = window.setInterval(() => {
      if (document.visibilityState === 'visible' && this.canvas.threadId()) {
        this.canvas.reconcile();
      }
    }, CANVAS_POPOUT_RECONCILE_MS);
    this.destroyRef.onDestroy(() => {
      if (this.reconcileTimer !== null) clearInterval(this.reconcileTimer);
      this.reconcileTimer = null;
    });
  }
}
