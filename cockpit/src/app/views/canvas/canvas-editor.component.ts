import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  effect,
  inject,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';
import {TranslocoPipe} from '@jsverse/transloco';
import {
  disposeMonacoEditor,
  MonacoCodeEditor,
  MonacoDisposable,
  MonacoEditorLoaderService,
  MonacoTextModel,
  monacoLanguageFromPath,
  preferredMonacoTheme,
} from '../../core/services/monaco-editor-loader.service';

/** Lazy, same-origin Monaco host for an editable Canvas text source. */
@Component({
  selector: 'app-canvas-editor',
  standalone: true,
  imports: [TranslocoPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div #editorHost class="canvas-editor" [attr.aria-label]="'canvas.editor.label' | transloco"></div>
    @if (loading()) {
      <div class="canvas-editor-state" role="status">
        <span class="canvas-editor-spinner" aria-hidden="true"></span>
        <span>{{ 'canvas.editor.loading' | transloco }}</span>
      </div>
    } @else if (failed()) {
      <div class="canvas-editor-state" role="alert">
        {{ 'canvas.editor.unavailable' | transloco }}
      </div>
    }
  `,
  styles: `
    :host {
      position: relative;
      display: block;
      width: 100%;
      height: 100%;
      min-width: 0;
      min-height: 280px;
      background: var(--surface-0);
    }

    .canvas-editor {
      width: 100%;
      height: 100%;
      min-height: 280px;
    }

    .canvas-editor-state {
      position: absolute;
      inset: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      padding: 20px;
      color: var(--text-muted);
      background: var(--surface-0);
    }

    .canvas-editor-spinner {
      width: 14px;
      height: 14px;
      border: 2px solid var(--border-color);
      border-top-color: var(--accent-color);
      border-radius: var(--radius-full);
      animation: canvas-editor-spin 0.8s linear infinite;
    }

    @keyframes canvas-editor-spin {
      to { transform: rotate(360deg); }
    }

    @media (prefers-reduced-motion: reduce) {
      .canvas-editor-spinner { animation: none; }
    }
  `,
})
export class CanvasEditorComponent {
  readonly value = input('');
  readonly path = input.required<string>();
  readonly disabled = input(false);
  readonly valueChange = output<string>();
  readonly editorFocus = output<void>();
  readonly editorBlur = output<void>();

  readonly loading = signal(true);
  readonly failed = signal(false);

  private readonly loader = inject(MonacoEditorLoaderService);
  private readonly destroyRef = inject(DestroyRef);
  private readonly host = viewChild<ElementRef<HTMLDivElement>>('editorHost');
  private editor: MonacoCodeEditor | null = null;
  private model: MonacoTextModel | null = null;
  private listeners: MonacoDisposable[] = [];
  private mountedPath: string | null = null;
  private requestToken: symbol | null = null;
  private applyingExternalValue = false;

  constructor() {
    effect(() => {
      const host = this.host()?.nativeElement;
      const path = this.path();
      const value = this.value();
      const disabled = this.disabled();
      if (!host) return;

      if (this.mountedPath !== path || !this.editor) {
        void this.mount(host, path, value, disabled);
      } else {
        this.syncExternalValue(value);
      }
    });

    this.destroyRef.onDestroy(() => this.dispose());
  }

  focus(): void {
    this.editor?.focus();
  }

  private async mount(
    host: HTMLDivElement,
    path: string,
    value: string,
    disabled: boolean,
  ): Promise<void> {
    const token = Symbol('canvas-monaco-load');
    this.requestToken = token;
    this.loading.set(true);
    this.failed.set(false);
    try {
      const monaco = await this.loader.load();
      if (this.requestToken !== token || this.host()?.nativeElement !== host) return;

      this.disposeEditorOnly();
      host.replaceChildren();
      const model = monaco.editor.createModel(value, monacoLanguageFromPath(path));
      const editor = monaco.editor.create(host, {
        automaticLayout: true,
        model,
        readOnly: disabled,
        scrollBeyondLastLine: false,
        minimap: {enabled: false},
        wordWrap: path.toLowerCase().endsWith('.md') ? 'on' : 'off',
        theme: preferredMonacoTheme(),
      });
      this.model = model;
      this.editor = editor;
      this.mountedPath = path;
      this.listeners = [
        editor.onDidChangeModelContent(() => {
          if (!this.applyingExternalValue) this.valueChange.emit(editor.getValue());
        }),
        editor.onDidFocusEditorText(() => this.editorFocus.emit()),
        editor.onDidBlurEditorText(() => this.editorBlur.emit()),
      ];
      this.loading.set(false);
    } catch {
      if (this.requestToken !== token) return;
      this.loading.set(false);
      this.failed.set(true);
    }
  }

  private syncExternalValue(value: string): void {
    const editor = this.editor;
    if (!editor) return;
    this.applyingExternalValue = true;
    try {
      syncCanvasMonacoEditor(editor, value, this.disabled());
    } finally {
      this.applyingExternalValue = false;
    }
  }

  private disposeEditorOnly(): void {
    for (const listener of this.listeners) listener.dispose();
    this.listeners = [];
    disposeMonacoEditor(this.editor, this.model ? [this.model] : []);
    this.editor = null;
    this.model = null;
  }

  private dispose(): void {
    this.requestToken = null;
    this.disposeEditorOnly();
    this.mountedPath = null;
  }
}

/** Apply reactive options/content without losing the user's scroll/selection. */
export function syncCanvasMonacoEditor(
  editor: MonacoCodeEditor,
  value: string,
  readOnly: boolean,
): void {
  editor.updateOptions({readOnly});
  if (editor.getValue() === value) return;
  const viewState = editor.saveViewState();
  editor.setValue(value);
  if (viewState) editor.restoreViewState(viewState);
}
