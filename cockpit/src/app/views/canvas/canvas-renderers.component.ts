import {ChangeDetectionStrategy, Component, computed, effect, inject, input, signal} from '@angular/core';
import {DomSanitizer, SafeHtml} from '@angular/platform-browser';
import {TranslocoPipe} from '@jsverse/transloco';
import {AppIconComponent} from '../../ui/icon';
import {AppIconButtonComponent} from '../../ui/icon-button';
import {
  buildCanvasFrameWrapper,
  renderCanvasInteractiveHtml,
  renderCanvasMarkdown,
  renderCanvasStaticHtml,
  shouldResetCanvasImageZoom,
} from './canvas-rendering';

const CANVAS_DOCUMENT_BLOB_TYPE = 'text/html;charset=utf-8';

function createCanvasDocumentUrl(html: string): string | null {
  if (
    typeof Blob === 'undefined' ||
    typeof URL === 'undefined' ||
    typeof URL.createObjectURL !== 'function'
  ) {
    return null;
  }
  return URL.createObjectURL(new Blob([html], {type: CANVAS_DOCUMENT_BLOB_TYPE}));
}

function revokeCanvasDocumentUrl(url: string): void {
  if (typeof URL.revokeObjectURL === 'function') URL.revokeObjectURL(url);
}

@Component({
  selector: 'app-canvas-markdown-renderer',
  standalone: true,
  imports: [TranslocoPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (rendered().errorCode) {
      <div class="canvas-render-limit" role="alert">{{ 'canvas.contentTooComplex' | transloco }}</div>
    } @else {
      <article class="canvas-markdown" [innerHTML]="html()" (click)="openLink($event)"></article>
    }
  `,
  styles: `
    :host { display: block; min-width: 0; }
    .canvas-markdown { max-width: 78ch; margin: 0 auto; color: var(--text-primary); line-height: 1.65; }
    .canvas-render-limit { padding: 20px; color: var(--text-muted); }
  `,
})
export class CanvasMarkdownRendererComponent {
  readonly content = input('');
  private readonly sanitizer = inject(DomSanitizer);
  readonly rendered = computed(() => renderCanvasMarkdown(this.content()));
  readonly html = computed<SafeHtml>(() =>
    this.sanitizer.bypassSecurityTrustHtml(this.rendered().html),
  );

  openLink(event: MouseEvent): void {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const link = target.closest<HTMLAnchorElement>('a[data-canvas-external]');
    const href = link?.dataset['canvasExternal'];
    if (!href) return;
    event.preventDefault();
    window.open(href, '_blank', 'noopener,noreferrer');
  }
}

@Component({
  selector: 'app-canvas-text-renderer',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<pre class="canvas-text" tabindex="0"><code>{{ content() }}</code></pre>`,
  styles: `
    :host { display: block; min-width: 0; }
    .canvas-text {
      min-height: 100%;
      margin: 0;
      padding: 20px;
      box-sizing: border-box;
      color: var(--text-primary);
      background: var(--code-bg, var(--surface-0));
      font: 13px/1.6 var(--font-mono, ui-monospace, monospace);
      white-space: pre;
      tab-size: 4;
      overflow: auto;
    }
  `,
})
export class CanvasTextRendererComponent {
  readonly content = input('');
}

@Component({
  selector: 'app-canvas-image-renderer',
  standalone: true,
  imports: [TranslocoPipe, AppIconComponent, AppIconButtonComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="image-toolbar">
      <app-icon-button size="sm" [ariaLabel]="'canvas.image.zoomOut' | transloco"
                       [tooltip]="'canvas.image.zoomOut' | transloco"
                       [disabled]="zoom() <= 0.25" (clicked)="changeZoom(-0.25)">
        <app-icon size="sm">zoom_out</app-icon>
      </app-icon-button>
      <span class="zoom-value">{{ zoomPercent() }}%</span>
      <app-icon-button size="sm" [ariaLabel]="'canvas.image.zoomIn' | transloco"
                       [tooltip]="'canvas.image.zoomIn' | transloco"
                       [disabled]="zoom() >= 4" (clicked)="changeZoom(0.25)">
        <app-icon size="sm">zoom_in</app-icon>
      </app-icon-button>
    </div>
    <div class="image-stage">
      <img [src]="src()" [alt]="alt()" [style.width.%]="zoom() * 100" />
    </div>
  `,
  styles: `
    :host { display: flex; flex-direction: column; min-height: 100%; min-width: 0; }
    .image-toolbar {
      position: sticky;
      top: 0;
      z-index: 1;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      padding: 6px;
      background: color-mix(in srgb, var(--panel-bg) 92%, transparent);
      border-bottom: 1px solid var(--border-color);
    }
    .zoom-value { min-width: 4ch; text-align: center; font-size: 11px; color: var(--text-muted); }
    .image-stage { flex: 1; display: grid; place-items: center; padding: 20px; overflow: auto; }
    img { display: block; max-width: none; height: auto; object-fit: contain; }
  `,
})
export class CanvasImageRendererComponent {
  readonly src = input('');
  readonly alt = input('');
  readonly sourceKey = input('');
  readonly zoom = signal(1);
  readonly zoomPercent = computed(() => Math.round(this.zoom() * 100));
  private lastSourceKey: string | null = null;

  constructor() {
    effect(() => {
      const key = this.sourceKey();
      if (shouldResetCanvasImageZoom(this.lastSourceKey, key)) this.zoom.set(1);
      this.lastSourceKey = key;
    });
  }

  changeZoom(delta: number): void {
    this.zoom.update(value => Math.max(0.25, Math.min(4, value + delta)));
  }
}

@Component({
  selector: 'app-canvas-html-renderer',
  standalone: true,
  imports: [TranslocoPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (rendered().errorCode) {
      <div class="canvas-render-limit" role="alert">{{ 'canvas.contentTooComplex' | transloco }}</div>
    } @else if (srcdoc(); as frameDocument) {
      <iframe sandbox loading="lazy" referrerpolicy="no-referrer"
              [title]="title()" [srcdoc]="frameDocument"></iframe>
    }
  `,
  styles: `
    :host { display: block; width: 100%; height: 100%; min-height: 320px; }
    iframe { display: block; width: 100%; height: 100%; min-height: 320px; border: 0; background: white; }
    .canvas-render-limit { padding: 20px; color: var(--text-muted); }
  `,
})
export class CanvasHtmlRendererComponent {
  readonly content = input('');
  readonly title = input('');
  private readonly sanitizer = inject(DomSanitizer);
  readonly rendered = computed(() => renderCanvasStaticHtml(this.content()));
  readonly srcdoc = signal<SafeHtml | null>(null);

  constructor() {
    effect(onCleanup => {
      const rendered = this.rendered();
      const title = this.title();
      if (rendered.errorCode) {
        this.srcdoc.set(null);
        return;
      }
      const contentUrl = createCanvasDocumentUrl(rendered.html);
      if (!contentUrl) {
        this.srcdoc.set(null);
        return;
      }
      const wrapper = buildCanvasFrameWrapper(contentUrl, title, false);
      if (!wrapper) {
        revokeCanvasDocumentUrl(contentUrl);
        this.srcdoc.set(null);
        return;
      }
      this.srcdoc.set(this.sanitizer.bypassSecurityTrustHtml(wrapper));
      onCleanup(() => revokeCanvasDocumentUrl(contentUrl));
    });
  }
}

@Component({
  selector: 'app-canvas-interactive-html-renderer',
  standalone: true,
  imports: [TranslocoPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (rendered().errorCode) {
      <div class="canvas-render-limit" role="alert">{{ 'canvas.contentTooComplex' | transloco }}</div>
    } @else if (srcdoc(); as frameDocument) {
      <iframe sandbox="allow-scripts" loading="lazy" referrerpolicy="no-referrer"
              allow="camera 'none'; microphone 'none'; geolocation 'none';
                     clipboard-read 'none'; clipboard-write 'none'"
              [title]="title()" [srcdoc]="frameDocument"></iframe>
    }
  `,
  styles: `
    :host { display: block; width: 100%; height: 100%; min-height: 320px; }
    iframe { display: block; width: 100%; height: 100%; min-height: 320px; border: 0; background: white; }
    .canvas-render-limit { padding: 20px; color: var(--text-muted); }
  `,
})
export class CanvasInteractiveHtmlRendererComponent {
  readonly content = input('');
  readonly title = input('');
  private readonly sanitizer = inject(DomSanitizer);
  readonly rendered = computed(() => renderCanvasInteractiveHtml(this.content()));
  readonly srcdoc = signal<SafeHtml | null>(null);

  constructor() {
    effect(onCleanup => {
      const rendered = this.rendered();
      const title = this.title();
      if (rendered.errorCode) {
        this.srcdoc.set(null);
        return;
      }
      const contentUrl = createCanvasDocumentUrl(rendered.html);
      if (!contentUrl) {
        this.srcdoc.set(null);
        return;
      }
      const wrapper = buildCanvasFrameWrapper(contentUrl, title, true);
      if (!wrapper) {
        revokeCanvasDocumentUrl(contentUrl);
        this.srcdoc.set(null);
        return;
      }
      this.srcdoc.set(this.sanitizer.bypassSecurityTrustHtml(wrapper));
      onCleanup(() => revokeCanvasDocumentUrl(contentUrl));
    });
  }
}
