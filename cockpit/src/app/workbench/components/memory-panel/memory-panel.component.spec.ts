import {describe, expect, it, beforeAll, beforeEach} from 'vitest';
import {TestBed} from '@angular/core/testing';
import {ɵresolveComponentResources} from '@angular/core';
import {MemoryPanelComponent} from './memory-panel.component';
import {ApiService} from '../../../core/services/api.service';
import {DataService} from '../../../core/services/data.service';

describe('MemoryPanelComponent colors', () => {
  let c: MemoryPanelComponent;

  // The template renders <app-spinner>, whose @Component uses an external
  // `styleUrl`. The Angular CLI normally inlines styleUrl content at build
  // time; this project's vitest setup JIT-compiles raw TS with no such
  // transform, so `TestBed.configureTestingModule` throws ("Component
  // 'AppSpinnerComponent' is not resolved") unless the pending resource
  // queue is drained first. Nothing here renders the template, so an
  // empty-string resolver is enough to unblock compilation.
  beforeAll(async () => {
    await ɵresolveComponentResources(() => Promise.resolve(''));
  });

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [MemoryPanelComponent],
      providers: [
        // The constructor eagerly injects these two services and the
        // effect() reads data.currentJobId(). The real ApiService pulls in
        // HttpClient -> TranslocoService -> AppToastService, none of which
        // are wired up in this unit test, and the color helpers under test
        // never touch either service, so light stand-ins are enough to
        // satisfy DI.
        {provide: ApiService, useValue: {}},
        {provide: DataService, useValue: {currentJobId: () => null}},
      ],
    });
    c = TestBed.createComponent(MemoryPanelComponent).componentInstance;
  });

  it('maps memory types to ramp / semantic tokens', () => {
    expect(c.typeColors.factual).toBe('var(--cat-6)');
    expect(c.typeColors.error_solution).toBe('var(--danger)');
  });

  it('maps memory sources, with tool_error semantic', () => {
    expect(c.sourceColorMap.observer).toBe('var(--cat-5)');
    expect(c.sourceColorMap.tool_error).toBe('var(--danger)');
  });

  it('maps importance to semantic thresholds', () => {
    expect(c.importanceColor(0.9)).toBe('var(--success)');
    expect(c.importanceColor(0.6)).toBe('var(--warning)');
    expect(c.importanceColor(0.2)).toBe('var(--danger)');
  });

  it('tints a token via color-mix (not hex-alpha concat)', () => {
    expect(c.tint('var(--cat-6)')).toBe('color-mix(in srgb, var(--cat-6) 20%, transparent)');
  });
});
