import {WritableSignal, signal, ɵresolveComponentResources} from '@angular/core';
import {ComponentFixture, TestBed} from '@angular/core/testing';
import {TranslocoTestingModule} from '@jsverse/transloco';
import {beforeAll, beforeEach, describe, expect, it, vi} from 'vitest';

/**
 * `<app-copy-field>` has a required signal input (`value`) and its own
 * template nests `<app-icon-button>`/`<app-icon>`, which have the same
 * problem one level deeper — see copy-field.stub.ts for the full
 * explanation. This project's vitest pipeline never runs components through
 * ngtsc, so signal-input metadata never reaches JIT and the real component
 * cannot be mounted here regardless of how its inputs are set.
 */
vi.mock('../../../ui/copy-field', () => import('./copy-field.stub'));

import {SshConnectPanelComponent} from './ssh-connect-panel.component';

type SeedableInput = 'handle' | 'apiHost' | 'sshHost' | 'hostKeyFingerprint';

describe('SshConnectPanelComponent', () => {
  beforeAll(async () => {
    // The component carries an external `styleUrl`, which JIT compilation in
    // jsdom refuses to resolve on its own (same gap as
    // ssh-keys-page.component.spec.ts and several others).
    await ɵresolveComponentResources(() => Promise.resolve(''));
  });

  beforeEach(async () => {
    // Deliberately EMPTY catalogue: every `chat.ssh.*` / `common.*` key used
    // here is therefore "missing", so TranslocoPipe renders the raw key
    // rather than a translation. That is what makes the "warns about the
    // concurrency seams" assertion below meaningful under ruling P-6 — see
    // its comment.
    await TestBed.configureTestingModule({
      imports: [
        SshConnectPanelComponent,
        TranslocoTestingModule.forRoot({
          langs: {en: {}},
          translocoConfig: {availableLangs: ['en'], defaultLang: 'en'},
        }),
      ],
    }).compileComponents();
  });

  /**
   * `fixture.componentRef.setInput(...)` logs NG0303 and silently does
   * nothing here — this pipeline drops signal-input metadata (same gap
   * documented in multi-select.component.spec.ts). Inputs are seeded by
   * replacing the `InputSignal` field with a plain `signal()` before the
   * first change detection instead; every computed here only ever calls
   * `this.<input>()`, so it never notices the swap.
   */
  function make(inputs: Record<SeedableInput, unknown>): ComponentFixture<SshConnectPanelComponent> {
    const fixture = TestBed.createComponent(SshConnectPanelComponent);
    const instance = fixture.componentInstance as unknown as Record<SeedableInput, WritableSignal<unknown>>;
    (Object.entries(inputs) as [SeedableInput, unknown][]).forEach(([k, v]) => {
      instance[k] = signal(v);
    });
    fixture.detectChanges();
    return fixture;
  }

  const ready = {
    handle: 's-7f3a91c2',
    apiHost: 'api.srw.works',
    sshHost: 'ssh.srw.works',
    hostKeyFingerprint: 'SHA256:bbb',
  };

  it('renders the generated config block', () => {
    const fixture = make(ready);
    expect(fixture.componentInstance.configBlock()).toContain('Host srw-s-7f3a91c2');
  });

  it('shows the gateway host key fingerprint for first-connect verification', () => {
    expect(make(ready).nativeElement.textContent).toContain('SHA256:bbb');
  });

  it('offers the JetBrains listener command separately', () => {
    expect(make(ready).componentInstance.jetBrainsCommand()).toContain('--listen');
  });

  it('hides itself when no handle exists yet', () => {
    const fixture = make({ ...ready, handle: null });
    expect(fixture.componentInstance.available()).toBe(false);
  });

  it('hides itself when the deployment has no gateway', () => {
    const fixture = make({ ...ready, sshHost: '' });
    expect(fixture.componentInstance.available()).toBe(false);
  });

  it('degrades to a message rather than throwing on a malformed handle', () => {
    const fixture = make({ ...ready, handle: 's-BAD\nProxyCommand x' });
    expect(fixture.componentInstance.available()).toBe(false);
    expect(fixture.componentInstance.configBlock()).toBe('');
  });

  it('warns about the concurrency seams', () => {
    // Documentation, not a fix: edits over SSH land in the agent's next turn
    // commit and a rewind discards them. Asserted on the i18n KEY
    // (chat.ssh.seams.rewindWarning), not translated copy: TestBed here
    // never loads a translation catalogue, so TranslocoPipe renders the raw
    // key — naming the key itself with "rewind" keeps this assertion
    // meaningful under both rendering modes (ruling P-6). The real rendered
    // sentence is confirmed in a browser by Task 7's live gate.
    expect(make(ready).nativeElement.textContent.toLowerCase()).toContain('rewind');
  });
});
