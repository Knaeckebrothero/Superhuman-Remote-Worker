import {describe, expect, it, beforeAll, beforeEach} from 'vitest';
import {TestBed} from '@angular/core/testing';
import {ɵresolveComponentResources} from '@angular/core';
import {AgentActivityComponent} from './agent-activity.component';
import {AuditTraceService} from '../../../core/services/audit-trace.service';
import {DataService} from '../../../core/services/data.service';
import {RequestService} from '../../services/request.service';

describe('AgentActivityComponent colors', () => {
  let c: AgentActivityComponent;

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
      imports: [AgentActivityComponent],
      providers: [
        // The constructor eagerly injects these three services. The real
        // AuditTraceService pulls in ApiService -> TranslocoService ->
        // HttpClient, none of which are wired up in this unit test, and
        // getStepColor/getToolColor never touch any of them, so light
        // stand-ins are enough to satisfy DI.
        {provide: AuditTraceService, useValue: {setJob: () => Promise.resolve()}},
        {provide: DataService, useValue: {currentJobId: () => null}},
        {provide: RequestService, useValue: {}},
      ],
    });
    c = TestBed.createComponent(AgentActivityComponent).componentInstance;
  });

  it('maps nominal step types to ramp tokens', () => {
    expect(c.getStepColor('llm')).toBe('var(--cat-4)');
    expect(c.getStepColor('tool')).toBe('var(--cat-7)');
    expect(c.getStepColor('initialize')).toBe('var(--cat-6)');
  });

  it('maps the error step type to the semantic danger token', () => {
    expect(c.getStepColor('error')).toBe('var(--danger)');
  });

  it('maps tool categories to ramp tokens', () => {
    expect(c.getToolColor('read_file')).toBe('var(--cat-6)'); // workspace
  });

  it('falls back to the muted token for unknown step types', () => {
    expect(c.getStepColor('nonsense' as never)).toBe('var(--text-muted)');
  });
});
