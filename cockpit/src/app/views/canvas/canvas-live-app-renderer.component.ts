import {ChangeDetectionStrategy, Component, Input} from '@angular/core';
import {SafeResourceUrl} from '@angular/platform-browser';

/** Fixed host boundary for an isolated, server-minted Canvas application URL. */
@Component({
  selector: 'app-canvas-live-app-renderer',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="live-app-warning" role="note">{{ warning }}</div>
    <iframe
      sandbox="allow-scripts allow-same-origin allow-forms"
      referrerpolicy="no-referrer"
      allow="camera 'none'; microphone 'none'; geolocation 'none'; clipboard-read 'none'; clipboard-write 'none'"
      [title]="title"
      [src]="src">
    </iframe>
  `,
  styles: `
    :host {
      display: flex;
      flex-direction: column;
      width: 100%;
      height: 100%;
      min-height: 320px;
    }
    .live-app-warning {
      flex: 0 0 auto;
      padding: 6px 10px;
      color: var(--text-primary);
      background: color-mix(in srgb, var(--warning-color, #f59e0b) 12%, transparent);
      border-bottom: 1px solid color-mix(in srgb, var(--warning-color, #f59e0b) 35%, transparent);
      font-size: 12px;
      font-weight: 600;
      line-height: 1.35;
    }
    iframe {
      display: block;
      flex: 1 1 auto;
      width: 100%;
      height: 100%;
      min-height: 320px;
      border: 0;
      background: white;
    }
  `,
})
export class CanvasLiveAppRendererComponent {
  @Input({required: true}) src!: SafeResourceUrl;
  @Input() title = '';
  @Input() warning = '';
}
