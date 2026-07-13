import {describe, expect, it} from 'vitest';
import {CanvasState} from '../../core/models/canvas.model';
import {ToolCardAction} from '../../core/models/tool-card.model';
import {canvasToolCardContext} from './tool-card.component';

function state(revision: number): CanvasState {
  return {
    canvas_id: 'main',
    source: {type: 'workspace_file', path: 'output/report.md'},
    title: 'Report',
    renderer: 'markdown',
    editable: false,
    alt_text: null,
    presentation_revision: revision,
    source_version: `sha256:${revision}`,
    status: 'ready',
    capabilities: {can_edit: false, can_pop_out: false, can_take_control: false},
    updated_at: '2026-07-13T10:00:00Z',
  };
}

describe('set_canvas tool-card context', () => {
  const action: ToolCardAction = {kind: 'open_canvas', presentationRevision: 5};

  it('labels a historical presentation as current, replaced, or unavailable', () => {
    expect(canvasToolCardContext(action, state(5))).toBe('current');
    expect(canvasToolCardContext(action, state(8))).toBe('replaced');
    expect(canvasToolCardContext(action, null)).toBe('unavailable');
    expect(canvasToolCardContext(action, {...state(8), source: null, status: 'cleared'}))
      .toBe('unavailable');
  });

  it('contains no restorable URL and always denotes opening the current stage', () => {
    expect(canvasToolCardContext({kind: 'open_canvas'}, state(8))).toBe('currentStage');
    expect(Object.keys(action)).toEqual(['kind', 'presentationRevision']);
    expect(JSON.stringify(action)).not.toContain('/api/');
  });
});
