import {Injectable} from '@angular/core';

export interface MonacoDisposable {
  dispose(): void;
}

export interface MonacoTextModel extends MonacoDisposable {
  getValue(): string;
  setValue(value: string): void;
}

export interface MonacoCodeEditor extends MonacoDisposable {
  getModel(): MonacoTextModel | null;
  getValue(): string;
  setValue(value: string): void;
  focus(): void;
  saveViewState(): unknown;
  restoreViewState(state: unknown): void;
  updateOptions(options: Readonly<Record<string, unknown>>): void;
  onDidChangeModelContent(listener: () => void): MonacoDisposable;
  onDidFocusEditorText(listener: () => void): MonacoDisposable;
  onDidBlurEditorText(listener: () => void): MonacoDisposable;
}

export interface MonacoDiffEditor extends MonacoDisposable {
  setModel(models: {original: MonacoTextModel; modified: MonacoTextModel}): void;
}

export interface MonacoEditorApi {
  editor: {
    createModel(value: string, language?: string): MonacoTextModel;
    create(container: HTMLElement, options: Readonly<Record<string, unknown>>): MonacoCodeEditor;
    createDiffEditor(
      container: HTMLElement,
      options: Readonly<Record<string, unknown>>,
    ): MonacoDiffEditor;
  };
}

/**
 * One lazy Monaco entry point shared by Canvas and the diff review.
 *
 * Monaco is served from same-origin static assets rather than bundled into the
 * initial Cockpit chunk. Keeping the promise here also prevents two editor
 * surfaces from racing separate AMD loader initializations.
 */
@Injectable({providedIn: 'root'})
export class MonacoEditorLoaderService {
  private modulePromise: Promise<MonacoEditorApi> | null = null;

  load(): Promise<MonacoEditorApi> {
    this.modulePromise ??= this.initialize();
    return this.modulePromise;
  }

  private async initialize(): Promise<MonacoEditorApi> {
    const loaderModule = await import('@monaco-editor/loader');
    const loader = loaderModule.default;
    loader.config({paths: {vs: 'monaco/vs'}});
    return await loader.init() as MonacoEditorApi;
  }
}

/** Best-effort language mapping shared by every workspace-backed editor. */
export function monacoLanguageFromPath(path: string): string {
  const ext = path.split('.').pop()?.toLowerCase() ?? '';
  const languages: Readonly<Record<string, string>> = {
    bash: 'shell',
    c: 'c',
    cc: 'cpp',
    cpp: 'cpp',
    css: 'css',
    go: 'go',
    h: 'c',
    hpp: 'cpp',
    html: 'html',
    java: 'java',
    js: 'javascript',
    json: 'json',
    jsx: 'javascript',
    kt: 'kotlin',
    latex: 'latex',
    md: 'markdown',
    markdown: 'markdown',
    php: 'php',
    py: 'python',
    rb: 'ruby',
    rs: 'rust',
    scss: 'scss',
    sh: 'shell',
    sql: 'sql',
    tex: 'latex',
    ts: 'typescript',
    tsx: 'typescript',
    txt: 'plaintext',
    xml: 'xml',
    yaml: 'yaml',
    yml: 'yaml',
  };
  return languages[ext] ?? 'plaintext';
}

export function preferredMonacoTheme(): 'vs' | 'vs-dark' {
  if (typeof document === 'undefined') return 'vs';
  return document.body.classList.contains('theme-senate') ? 'vs-dark' : 'vs';
}

/** Dispose an editor and every explicitly-created model it owns. */
export function disposeMonacoEditor(
  editor: MonacoDisposable | null,
  models: readonly MonacoDisposable[] = [],
): void {
  editor?.dispose();
  for (const model of models) model.dispose();
}
