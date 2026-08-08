import {beforeAll, describe, expect, it} from 'vitest';
import {signal, ɵresolveComponentResources} from '@angular/core';
import {TestBed} from '@angular/core/testing';
import {TranslocoTestingModule} from '@jsverse/transloco';
import en from '../../../assets/i18n/en.json';
import {ExecutionGroupComponent} from './execution-group.component';
import {UserService} from '../../core/services/user.service';

/**
 * Where the workspace backend actually SITS.
 *
 * The control was moved out of Advanced → Workspace to be a level-1 setting in
 * the EXECUTION group, between Permission Mode (Autonomy, in job mode) and
 * Image Quality. Placement is the deliverable, so these mount the component and
 * assert against rendered label order rather than component state — a spec that
 * only reads signals would pass with the row anywhere on the page, or nowhere.
 */
function mount(options: {mode?: string; canUseVm?: boolean} = {}) {
  TestBed.configureTestingModule({
    imports: [
      ExecutionGroupComponent,
      TranslocoTestingModule.forRoot({
        langs: {en},
        translocoConfig: {availableLangs: ['en'], defaultLang: 'en'},
      }),
    ],
    providers: [
      {
        provide: UserService,
        useValue: {currentUser: signal(options.canUseVm ? {is_admin: true} : {is_admin: false})},
      },
    ],
  });
  const fixture = TestBed.createComponent(ExecutionGroupComponent);
  // Signal inputs cannot be set through setInput() in this pipeline; see
  // tools-group.render.spec.ts for the reason.
  Object.defineProperty(fixture.componentInstance, 'mode', {value: () => options.mode ?? 'session'});
  fixture.detectChanges();
  return fixture;
}

function labels(fixture: {nativeElement: unknown}): string[] {
  return Array.from((fixture.nativeElement as HTMLElement).querySelectorAll('.field-label'))
    .map((el) => (el.textContent ?? '').trim());
}

function optionValues(fixture: {nativeElement: unknown}, labelText: string): string[] {
  const row = Array.from((fixture.nativeElement as HTMLElement).querySelectorAll('.field-row'))
    .find((el) => (el.querySelector('.field-label')?.textContent ?? '').trim() === labelText);
  if (!row) throw new Error(`no row labelled ${labelText}; saw: ${labels(fixture).join(', ')}`);
  return Array.from(row.querySelectorAll('option')).map((o) => (o as HTMLOptionElement).value);
}

describe('ExecutionGroupComponent — workspace backend placement', () => {
  beforeAll(async () => {
    await ɵresolveComponentResources(() => Promise.resolve(''));
  });

  it('sits between Permission Mode and Image Quality in session mode', () => {
    const seen = labels(mount({mode: 'session'}));
    expect(seen).toContain('Workspace');
    expect(seen.indexOf('Workspace')).toBeGreaterThan(seen.indexOf('Permission Mode'));
    expect(seen.indexOf('Workspace')).toBeLessThan(seen.indexOf('Image Quality'));
  });

  it('sits between Autonomy and Image Quality in job mode', () => {
    const seen = labels(mount({mode: 'job'}));
    expect(seen.indexOf('Workspace')).toBeGreaterThan(seen.indexOf('Autonomy'));
    expect(seen.indexOf('Workspace')).toBeLessThan(seen.indexOf('Image Quality'));
  });

  it('is absent in live mode — a running session\'s backend is fixed', () => {
    expect(labels(mount({mode: 'live'}))).not.toContain('Workspace');
  });

  it('offers the VM tier to a user who may run one', () => {
    expect(optionValues(mount({canUseVm: true}), 'Workspace'))
      .toEqual(['sandbox', 'vm', 'virtual', 'none']);
  });

  it('withholds the VM tier from a user who may not', () => {
    expect(optionValues(mount({canUseVm: false}), 'Workspace'))
      .toEqual(['sandbox', 'virtual', 'none']);
  });

  it('renders resolved copy, never a raw translation key', () => {
    const text = ((mount().nativeElement as HTMLElement).textContent ?? '');
    expect(text).not.toMatch(/agentSettings\.|advanced\./);
    expect(text).toContain('Container');
  });
});
