import {Component} from '@angular/core';
import {TestBed} from '@angular/core/testing';
import {TranslocoTestingModule} from '@jsverse/transloco';
import {afterEach, describe, expect, it} from 'vitest';
import {CanvasState} from '../../core/models/canvas.model';
import {ToolCardAction, ToolCardView} from '../../core/models/tool-card.model';
import {buildToolCardView} from '../../core/tools/tool-descriptors';
import {CanvasToolCardPresentationComponent} from './canvas-tool-card-presentation.component';
import {canvasToolCardContext} from './tool-card.component';

@Component({
  standalone: true,
  imports: [CanvasToolCardPresentationComponent],
  template: `
    <app-canvas-tool-card-presentation [presentation]="view.canvasPresentation"
                                       [action]="view.action!"
                                       contextLabel="current" [available]="true"
                                       (requested)="requested.push($event)" />
  `,
})
class ToolCardHost {
  view!: ToolCardView;
  requested: ToolCardAction[] = [];
}

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
  afterEach(() => TestBed.resetTestingModule());

  const action: ToolCardAction = {kind: 'open_canvas', presentationRevision: 5};

  it('labels a historical presentation as current, replaced, or unavailable', () => {
    expect(canvasToolCardContext(action, state(5))).toBe('current');
    expect(canvasToolCardContext(action, state(8))).toBe('replaced');
    expect(canvasToolCardContext(action, null)).toBe('unavailable');
    expect(canvasToolCardContext(action, {...state(8), source: null, status: 'cleared'}))
      .toBe('unavailable');
  });

  it('follows content identity over the revision when both are known', () => {
    // Re-binding a Canvas to a rebuilt workspace bumps the revision while
    // presenting byte-identical content. Comparing revisions alone made that
    // card claim it had been replaced.
    const hashed: ToolCardAction = {
      kind: 'open_canvas',
      presentationRevision: 5,
      sourceVersion: 'sha256:5',
    };
    const repinned = {...state(6), source_version: 'sha256:5'};
    expect(canvasToolCardContext(hashed, repinned)).toBe('current');

    // A genuinely different presentation is still replaced, even at the same
    // revision number.
    const different = {...state(5), source_version: 'sha256:other'};
    expect(canvasToolCardContext(hashed, different)).toBe('replaced');
  });

  it('falls back to the revision when either side lacks a content hash', () => {
    const hashed: ToolCardAction = {
      kind: 'open_canvas',
      presentationRevision: 5,
      sourceVersion: 'sha256:5',
    };
    expect(canvasToolCardContext(hashed, {...state(5), source_version: null}))
      .toBe('current');
    expect(canvasToolCardContext(hashed, {...state(9), source_version: null}))
      .toBe('replaced');
  });

  it('contains no restorable URL and always denotes opening the current stage', () => {
    expect(canvasToolCardContext({kind: 'open_canvas'}, state(8))).toBe('currentStage');
    expect(Object.keys(action)).toEqual(['kind', 'presentationRevision']);
    expect(JSON.stringify(action)).not.toContain('/api/');
  });

  it('renders the normalized source/title summary and emits the trusted action', async () => {
    TestBed.configureTestingModule({
      imports: [
        ToolCardHost,
        TranslocoTestingModule.forRoot({
          langs: {
            en: {
              toolCard: {
                status: {ok: 'completed'},
                titles: {set_canvas: 'Present in Canvas'},
                canvas: {
                  open: 'Open Canvas',
                  current: 'current',
                  sourceKind: {file: 'File', app: 'Live app', canvas: 'Canvas'},
                },
              },
            },
          },
          translocoConfig: {availableLangs: ['en'], defaultLang: 'en'},
        }),
      ],
    });
    await TestBed.compileComponents();
    const fixture = TestBed.createComponent(ToolCardHost);
    const view = buildToolCardView({
      tool: 'set_canvas',
      args: {source_type: 'workspace_file', path: 'output/report.md'},
      status: 'ok',
      result: JSON.stringify({
        presentation_revision: 5,
        title: 'Quarterly report',
        source: {type: 'workspace_file', path: 'output/report.md'},
      }),
    });
    fixture.componentInstance.view = view;
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();

    const root = fixture.nativeElement as HTMLElement;
    expect(root.querySelector('.tc-canvas__kind')?.textContent?.trim()).toBe('File');
    expect(root.querySelector('.tc-canvas__title')?.textContent?.trim()).toBe('Quarterly report');
    const open = root.querySelector('.tc-canvas__action') as HTMLButtonElement;
    expect(open.textContent?.trim()).toBe('Open Canvas');
    expect(open.disabled).toBe(false);
    open.click();
    expect(fixture.componentInstance.requested).toEqual([{
      kind: 'open_canvas',
      presentationRevision: 5,
    }]);
  });
});
