import {describe, expect, it, beforeEach} from 'vitest';
import {TestBed} from '@angular/core/testing';
import {LayoutPreviewComponent} from './layout-preview.component';

describe('LayoutPreviewComponent palette', () => {
  let c: LayoutPreviewComponent;
  beforeEach(() => {
    TestBed.configureTestingModule({imports: [LayoutPreviewComponent]});
    c = TestBed.createComponent(LayoutPreviewComponent).componentInstance;
  });
  it('uses the ramp tokens for its palette', () => {
    expect(c['colors']).toEqual([
      'var(--cat-1)','var(--cat-2)','var(--cat-3)','var(--cat-4)',
      'var(--cat-5)','var(--cat-6)','var(--cat-7)','var(--cat-8)',
    ]);
  });
  it('uses theme tokens for stroke and background', () => {
    expect(c.strokeColor).toBe('var(--surface-1)');
    expect(c.bgColor).toBe('var(--panel-bg)');
  });
});
