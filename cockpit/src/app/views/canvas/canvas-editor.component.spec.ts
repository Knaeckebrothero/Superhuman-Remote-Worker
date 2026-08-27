import {describe, expect, it, vi} from 'vitest';
import {
  disposeMonacoEditor,
  MonacoCodeEditor,
  MonacoDisposable,
  MonacoTextModel,
} from '../../core/services/monaco-editor-loader.service';
import {syncCanvasMonacoEditor} from './canvas-editor.component';

describe('Canvas Monaco adapter', () => {
  it('reacts to read-only changes and preserves selection/scroll across external content', () => {
    let value = '# Original';
    const editor = {
      getValue: vi.fn(() => value),
      setValue: vi.fn(next => value = next),
      updateOptions: vi.fn(),
      saveViewState: vi.fn(() => ({scrollTop: 42, cursor: 7})),
      restoreViewState: vi.fn(),
    } as unknown as MonacoCodeEditor;

    syncCanvasMonacoEditor(editor, '# Remote refresh', true);

    expect(editor.updateOptions).toHaveBeenCalledWith({readOnly: true});
    expect(editor.setValue).toHaveBeenCalledWith('# Remote refresh');
    expect(editor.restoreViewState).toHaveBeenCalledWith({scrollTop: 42, cursor: 7});

    syncCanvasMonacoEditor(editor, '# Remote refresh', false);
    expect(editor.updateOptions).toHaveBeenLastCalledWith({readOnly: false});
    expect(editor.setValue).toHaveBeenCalledOnce();
  });

  it('disposes the editor and every explicitly-created Monaco model', () => {
    const editor = {dispose: vi.fn()} as unknown as MonacoCodeEditor;
    const original = {dispose: vi.fn()} as unknown as MonacoTextModel;
    const modified = {dispose: vi.fn()} as unknown as MonacoDisposable;

    disposeMonacoEditor(editor, [original, modified]);

    expect(editor.dispose).toHaveBeenCalledOnce();
    expect(original.dispose).toHaveBeenCalledOnce();
    expect(modified.dispose).toHaveBeenCalledOnce();
  });
});
