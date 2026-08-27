import {TranslocoTestingModule} from '@jsverse/transloco';
import {Component} from '@angular/core';
import {TestBed} from '@angular/core/testing';
import {afterEach, describe, expect, it} from 'vitest';
import {
  CanvasLiveAppUnavailableComponent,
  canvasLiveAppFailureKind,
} from './canvas-live-app-unavailable.component';

const translations = {
  canvas: {
    app: {
      retry: 'Retry secure preview',
      failure: {
        disabled: {title: 'Preview disabled', body: 'Disabled here.'},
        unsupported: {title: 'Secure preview unsupported', body: 'Browser blocked it.'},
        temporary: {title: 'Preview unavailable', body: 'Try again.'},
        safeFallback: 'The app was not opened top-level. Use the authenticated IDE.',
      },
    },
  },
};

@Component({
  standalone: true,
  imports: [CanvasLiveAppUnavailableComponent],
  template: `
    <app-canvas-live-app-unavailable [errorCode]="errorCode" (retry)="onRetry()" />
  `,
})
class CanvasLiveAppUnavailableHost {
  errorCode = 'canvas_browser_unsupported';
  retries = 0;

  onRetry(): void {
    this.retries++;
  }
}

describe('Canvas live-app failure UX', () => {
  afterEach(() => TestBed.resetTestingModule());

  it('distinguishes deployment disablement from embedded-auth incompatibility', () => {
    expect(canvasLiveAppFailureKind('canvas_viewer_not_configured')).toBe('disabled');
    expect(canvasLiveAppFailureKind('canvas_browser_unsupported')).toBe('unsupported');
    expect(canvasLiveAppFailureKind('canvas_browser_storage_unavailable')).toBe('unsupported');
    expect(canvasLiveAppFailureKind('canvas_viewer_bootstrap_failed')).toBe('temporary');
    expect(canvasLiveAppFailureKind('canvas_viewer_create_failed')).toBe('temporary');
  });

  it('renders explicit no-top-level fallback guidance and a retry action', async () => {
    TestBed.configureTestingModule({
      imports: [
        CanvasLiveAppUnavailableHost,
        TranslocoTestingModule.forRoot({
          langs: {en: translations},
          translocoConfig: {availableLangs: ['en'], defaultLang: 'en'},
        }),
      ],
    });
    await TestBed.compileComponents();
    const fixture = TestBed.createComponent(CanvasLiveAppUnavailableHost);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();

    const root = fixture.nativeElement.querySelector('.live-app-unavailable') as HTMLElement;
    expect(root.dataset['failureKind']).toBe('unsupported');
    expect(root.textContent).toContain('Secure preview unsupported');
    expect(root.textContent).toContain('The app was not opened top-level');
    (root.querySelector('button') as HTMLButtonElement).click();
    expect(fixture.componentInstance.retries).toBe(1);
  });
});
