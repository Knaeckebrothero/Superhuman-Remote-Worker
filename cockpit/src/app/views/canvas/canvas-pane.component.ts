import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  effect,
  inject,
  input,
  output,
  viewChild,
} from '@angular/core';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';
import {CanvasService} from '../../core/services/canvas.service';
import {AppBadgeComponent} from '../../ui/badge';
import {AppButtonComponent} from '../../ui/button';
import {AppIconComponent} from '../../ui/icon';
import {AppIconButtonComponent} from '../../ui/icon-button';
import {AppSpinnerComponent} from '../../ui/spinner';
import {
  CanvasHtmlRendererComponent,
  CanvasImageRendererComponent,
  CanvasMarkdownRendererComponent,
  CanvasTextRendererComponent,
} from './canvas-renderers.component';
import {selectCanvasChromeState, selectCanvasRenderer} from './canvas-rendering';
import {CanvasContentController} from './canvas-content.controller';
import {CanvasEditController} from './canvas-edit.controller';
import {CanvasEditorComponent} from './canvas-editor.component';
import {CanvasViewerController} from './canvas-viewer.controller';
import {CanvasLiveAppRendererComponent} from './canvas-live-app-renderer.component';

@Component({
  selector: 'app-canvas-pane',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  providers: [CanvasContentController, CanvasEditController, CanvasViewerController],
  imports: [
    TranslocoPipe,
    AppBadgeComponent,
    AppButtonComponent,
    AppIconComponent,
    AppIconButtonComponent,
    AppSpinnerComponent,
    CanvasHtmlRendererComponent,
    CanvasImageRendererComponent,
    CanvasLiveAppRendererComponent,
    CanvasMarkdownRendererComponent,
    CanvasTextRendererComponent,
    CanvasEditorComponent,
  ],
  template: `
    <section class="canvas-shell" [attr.aria-label]="'canvas.paneLabel' | transloco">
      <header class="canvas-header">
        <div class="canvas-heading">
          @if (mobile()) {
            <app-icon-button #returnButton size="sm"
                             [ariaLabel]="'canvas.returnToChat' | transloco"
                             [tooltip]="'canvas.returnToChat' | transloco"
                             (clicked)="returnToChat.emit()">
              <app-icon size="sm">arrow_back</app-icon>
            </app-icon-button>
          }
          <app-icon class="canvas-mark" size="md">dashboard_customize</app-icon>
          <div class="canvas-title-group">
            <h2>{{ title() }}</h2>
            <div class="canvas-source" [title]="sourceSummary()">{{ sourceSummary() }}</div>
          </div>
        </div>
        <div class="canvas-trust">
          <app-badge tone="warning" size="xs">{{ 'canvas.untrusted' | transloco }}</app-badge>
          <span>{{ rendererLabel() }}</span>
        </div>
        <div class="canvas-actions">
          @if (editor.hasSession()) {
            <app-icon-button size="sm"
                             [ariaLabel]="(editor.editMode() ? 'canvas.editor.preview' : 'canvas.editor.edit') | transloco"
                             [tooltip]="(editor.editMode() ? 'canvas.editor.preview' : 'canvas.editor.edit') | transloco"
                             [disabled]="!editor.editMode() && !editor.canEnterEdit()"
                             (clicked)="toggleEditor()">
              <app-icon size="sm">{{ editor.editMode() ? 'preview' : 'edit' }}</app-icon>
            </app-icon-button>
          }
          @if (editor.dirty()) {
            <app-button size="sm" [loading]="editor.saveStatus() === 'saving'"
                        [disabled]="!editor.canSave()" (clicked)="editor.save()">
              {{ (editor.saveStatus() === 'saving' ? 'canvas.editor.saving' : 'canvas.editor.save') | transloco }}
            </app-button>
          }
          <app-icon-button size="sm" [ariaLabel]="'canvas.refresh' | transloco"
                           [tooltip]="'canvas.refresh' | transloco"
                           [loading]="canvas.loadStatus() === 'loading'"
                           (clicked)="canvas.reconcile()">
            <app-icon size="sm">refresh</app-icon>
          </app-icon-button>
          <app-icon-button size="sm" [ariaLabel]="'canvas.close' | transloco"
                           [tooltip]="'canvas.close' | transloco"
                           (clicked)="closeRequested.emit()">
            <app-icon size="sm">close</app-icon>
          </app-icon-button>
        </div>
      </header>

      <div class="canvas-announcement" aria-live="polite" aria-atomic="true">
        {{ statusText() }}
      </div>

      @if (editor.conflict(); as conflict) {
        <div class="canvas-edit-notice" role="alert">
          <app-icon size="sm">warning</app-icon>
          <div class="canvas-edit-notice__copy">
            <strong>{{ ('canvas.editor.conflict.' + conflict + '.title') | transloco }}</strong>
            <span>{{ ('canvas.editor.conflict.' + conflict + '.body') | transloco }}</span>
          </div>
          <div class="canvas-edit-notice__actions">
            <app-button size="sm" variant="ghost" (clicked)="editor.keepEditing()">
              {{ 'canvas.editor.keepEditing' | transloco }}
            </app-button>
            @if (isReloadConflict()) {
              <app-button size="sm" variant="warning" [loading]="editor.refreshPending()"
                          (clicked)="editor.loadCurrentVersion()">
                {{ reloadLabel() }}
              </app-button>
            }
          </div>
        </div>
      }
      @if (editor.remoteEditing()) {
        <div class="canvas-remote-editing" role="status">
          <app-icon size="sm">edit_note</app-icon>
          <span>{{ 'canvas.editor.remoteEditing' | transloco }}</span>
        </div>
      }
      @if (sourceNeedsRefresh() && !editor.conflict()) {
        <div class="canvas-source-refresh" role="alert">
          <app-icon size="sm">sync_problem</app-icon>
          <span>{{ 'canvas.editor.sourceRefresh' | transloco }}</span>
          <app-button size="sm" variant="warning" [loading]="editor.refreshPending()"
                      (clicked)="editor.refreshPresentedSource()">
            {{ 'canvas.editor.loadWorkspace' | transloco }}
          </app-button>
        </div>
      }

      <div #contentViewport id="canvas-content" class="canvas-content" tabindex="-1">
        @if (editor.editorMounted() && editor.hasSession()) {
          @if (editor.sessionPath(); as editPath) {
            <app-canvas-editor #canvasEditor [path]="editPath" [value]="editor.buffer()"
                               [class.canvas-editor--hidden]="!editor.editMode()"
                               [disabled]="editor.saveStatus() === 'saving'"
                               (valueChange)="editor.updateBuffer($event)"
                               (editorFocus)="editor.editorFocused()"
                               (editorBlur)="editor.editorBlurred()" />
          }
        }
        @if (!editor.editMode()) {
          @if (hasVisual()) {
            @switch (effectiveRenderer()) {
              @case ('markdown') {
                <app-canvas-markdown-renderer [content]="previewContent()" />
              }
              @case ('text') {
                <app-canvas-text-renderer [content]="previewContent()" />
              }
              @case ('html') {
                <app-canvas-html-renderer [content]="previewContent()" [title]="frameTitle()" />
              }
              @case ('image') {
                <app-canvas-image-renderer [src]="imageUrl()!" [alt]="imageAlt()"
                                           [sourceKey]="displaySourceKey()!" />
              }
              @case ('app') {
                <app-canvas-live-app-renderer [src]="viewer.frameUrl()!"
                                              [title]="liveAppFrameTitle()"
                                              [warning]="'canvas.app.untrustedWarning' | transloco"
                                              (frameBound)="viewer.bindFrame($event)"
                                              (frameUnbound)="viewer.unbindFrame($event)"
                                              (frameMessage)="viewer.handleFrameMessage($event)" />
              }
            }
            @if (showOverlay()) {
              <div class="canvas-overlay" role="status">
                @if (contentStatus() === 'loading' || canvas.loadStatus() === 'loading' ||
                     viewer.viewerStatus() === 'loading' || viewer.viewerStatus() === 'renewing') {
                  <app-spinner size="sm" tone="accent" />
                } @else {
                  <app-icon size="sm">info</app-icon>
                }
                <span>{{ statusText() }}</span>
              </div>
            }
          } @else {
            <div class="canvas-placeholder">
              @if (contentStatus() === 'loading' || canvas.loadStatus() === 'loading' ||
                   viewer.viewerStatus() === 'loading' || viewer.viewerStatus() === 'renewing') {
                <app-spinner size="md" tone="accent" />
              } @else {
                <app-icon size="xl">preview</app-icon>
              }
              <p>{{ emptyText() }}</p>
            </div>
          }
        }
      </div>
    </section>
  `,
  styleUrl: './canvas-pane.component.scss',
})
export class CanvasPaneComponent {
  readonly active = input(true);
  readonly mobile = input(false);
  readonly closeRequested = output<void>();
  readonly returnToChat = output<void>();
  readonly dirtyChange = output<boolean>();

  readonly canvas = inject(CanvasService);
  private readonly content = inject(CanvasContentController);
  readonly editor = inject(CanvasEditController);
  readonly viewer = inject(CanvasViewerController);
  private readonly transloco = inject(TranslocoService);
  private readonly contentViewport = viewChild<ElementRef<HTMLElement>>('contentViewport');
  private readonly canvasEditor = viewChild<CanvasEditorComponent>('canvasEditor');

  readonly displayRenderer = this.content.displayRenderer;
  readonly displayState = this.content.displayState;
  readonly displaySourceKey = this.content.displaySourceKey;
  readonly textContent = this.content.textContent;
  readonly contentEtag = this.content.contentEtag;
  readonly imageUrl = this.content.imageUrl;
  readonly contentStatus = this.content.contentStatus;
  readonly contentErrorCode = this.content.contentErrorCode;

  readonly state = this.canvas.state;
  readonly chromeState = computed(() =>
    selectCanvasChromeState(
      this.state(),
      this.editor.sessionState(),
      this.editor.hasSession() && !!(this.editor.dirty() || this.editor.conflict()),
    ),
  );
  readonly effectiveRenderer = computed(() => {
    if (this.editor.hasSession() && (this.editor.dirty() || this.editor.conflict())) {
      return this.editor.sessionRenderer();
    }
    const selected = selectCanvasRenderer(this.state());
    return selected === 'app' ? selected : this.displayRenderer();
  });
  readonly previewContent = computed(() =>
    this.editor.hasSession() && this.editor.dirty()
      ? this.editor.buffer()
      : this.textContent(),
  );
  readonly hasVisual = computed(() => {
    if (this.editor.editMode() && this.editor.hasSession()) return true;
    if (this.effectiveRenderer() === 'app') return this.viewer.frameUrl() !== null;
    if (this.effectiveRenderer() === 'image') return this.imageUrl() !== null;
    return this.effectiveRenderer() !== 'unsupported' &&
      (this.contentStatus() !== 'idle' || this.editor.hasSession());
  });
  readonly title = computed(() =>
    this.chromeState()?.title?.trim() || this.transloco.translate('canvas.defaultTitle'),
  );
  readonly sourceSummary = computed(() => {
    const source = this.chromeState()?.source;
    if (source?.type === 'workspace_file' && typeof source.path === 'string') return source.path;
    if (source?.type === 'workspace_app') {
      return typeof source.entry_path === 'string' && source.entry_path
        ? source.entry_path
        : this.transloco.translate('canvas.app.source');
    }
    return this.transloco.translate('canvas.noSource');
  });
  readonly rendererLabel = computed(() =>
    this.transloco.translate(`canvas.renderer.${this.effectiveRenderer()}`),
  );
  readonly imageAlt = computed(() =>
    this.displayState()?.alt_text?.trim() || this.transloco.translate('canvas.image.missingAlt'),
  );
  readonly frameTitle = computed(() =>
    this.transloco.translate('canvas.html.frameTitle', {title: this.title()}),
  );
  readonly liveAppFrameTitle = computed(() =>
    this.transloco.translate('canvas.app.frameTitle', {title: this.title()}),
  );
  readonly sourceNeedsRefresh = computed(() =>
    this.state()?.status === 'source_changed' || this.contentStatus() === 'source_changed',
  );
  readonly showOverlay = computed(() => {
    const state = this.state();
    return (
      this.hasVisual() &&
      !this.editor.editMode() &&
      (this.contentStatus() === 'loading' ||
        this.contentStatus() === 'source_changed' ||
        this.viewer.viewerStatus() === 'loading' ||
        this.viewer.viewerStatus() === 'renewing' ||
        this.viewer.viewerStatus() === 'error' ||
        this.canvas.loadStatus() === 'loading' ||
        state?.status === 'source_changed' ||
        state?.status === 'unavailable' ||
        state?.status === 'error' ||
        state?.status === 'ended')
    );
  });
  readonly statusText = computed(() => {
    const state = this.state();
    if (state?.status === 'source_changed' || this.contentStatus() === 'source_changed') {
      return this.transloco.translate('canvas.status.sourceChanged');
    }
    if (this.editor.saveStatus() === 'saved') {
      return this.transloco.translate('canvas.editor.saved');
    }
    if (this.editor.dirty()) return this.transloco.translate('canvas.editor.unsaved');
    if (this.contentStatus() === 'loading') return this.transloco.translate('canvas.status.loading');
    if (this.viewer.viewerStatus() === 'loading') {
      return this.transloco.translate('canvas.app.connecting');
    }
    if (this.viewer.viewerStatus() === 'renewing') {
      return this.transloco.translate('canvas.app.renewing');
    }
    if (this.viewer.viewerStatus() === 'error') {
      return this.transloco.translate('canvas.app.unavailable');
    }
    if (this.canvas.loadStatus() === 'loading') {
      return this.transloco.translate(this.hasVisual() ? 'canvas.status.updating' : 'canvas.status.loading');
    }
    if (this.contentStatus() === 'error') return this.transloco.translate('canvas.status.contentError');
    if (state?.status && state.status !== 'ready') {
      return this.transloco.translate(`canvas.status.${state.status}`);
    }
    if (this.hasVisual()) {
      return this.transloco.translate('canvas.status.readyRevision', {
        revision: state?.presentation_revision ?? 0,
      });
    }
    return '';
  });
  readonly emptyText = computed(() => {
    if (selectCanvasRenderer(this.state()) === 'unsupported') {
      return this.transloco.translate('canvas.unsupported');
    }
    return this.statusText() || this.transloco.translate('canvas.empty');
  });

  constructor() {
    effect(() => this.content.syncPresentation(
      this.active(),
      this.canvas.threadId(),
      this.state(),
      () => this.contentViewport()?.nativeElement.scrollTop ?? 0,
      scrollTop => {
        const viewport = this.contentViewport()?.nativeElement;
        if (viewport) viewport.scrollTop = scrollTop;
      },
    ));
    effect(() => this.editor.sync(
      this.active(),
      this.canvas.threadId(),
      this.state(),
      this.displayState(),
      this.textContent(),
      this.contentEtag(),
      this.contentStatus() === 'ready',
      this.contentStatus() === 'source_changed',
    ));
    effect(() => this.viewer.syncPresentation(
      this.active() && this.effectiveRenderer() === 'app',
      this.canvas.threadId(),
      this.state(),
      this.canvas.stateEtag(),
    ));
    effect(() => this.dirtyChange.emit(this.editor.dirty()));
  }

  focusContent(): void {
    if (this.editor.editMode()) {
      this.canvasEditor()?.focus();
      return;
    }
    this.contentViewport()?.nativeElement.focus({preventScroll: true});
  }

  toggleEditor(): void {
    if (this.editor.editMode()) {
      this.editor.showPreview();
      return;
    }
    this.editor.enterEdit();
    queueMicrotask(() => this.canvasEditor()?.focus());
  }

  isReloadConflict(): boolean {
    const conflict = this.editor.conflict();
    return conflict === 'content_changed' ||
      conflict === 'presentation_changed' ||
      conflict === 'replaced' ||
      conflict === 'cleared';
  }

  reloadLabel(): string {
    const key = this.editor.conflict() === 'cleared'
      ? 'canvas.editor.discardCleared'
      : 'canvas.editor.loadCurrent';
    return this.transloco.translate(key);
  }
}
