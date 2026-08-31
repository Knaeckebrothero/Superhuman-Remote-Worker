import {describe, expect, it, vi, beforeAll, beforeEach} from 'vitest';
import {signal, ɵresolveComponentResources} from '@angular/core';
import {TestBed} from '@angular/core/testing';
import {Router} from '@angular/router';
import {TranslocoTestingModule} from '@jsverse/transloco';
import {SshKeysPageComponent} from './ssh-keys-page.component';
import {SshKeysService} from '../../../core/services/ssh-keys.service';
import {SidebarService} from '../../../core/services/sidebar.service';

// The real catalogue (as project-list.component.spec.ts does), so this spec
// also proves the `settings.sshKeys.*` keys exist — a missing key renders
// as the key itself rather than failing to compile.
import en from '../../../../assets/i18n/en.json';

class FakeSshKeysService {
  keys = [
    {
      id: 'k1',
      name: 'laptop',
      key_type: 'ssh-ed25519',
      fingerprint: 'SHA256:aaa',
      created_at: '2026-08-01T00:00:00Z',
      last_used_at: null,
      disabled: false,
    },
  ];
  challenge = {
    challenge: 'srw-nonce',
    namespace: 'srw-ssh-key-registration',
    expires_at: '2026-08-28T00:05:00Z',
  };
  loadKeys = vi.fn().mockResolvedValue(this.keys);
  requestChallenge = vi.fn().mockResolvedValue(this.challenge);
  createKey = vi.fn().mockResolvedValue(this.keys[0]);
  deleteKey = vi.fn().mockResolvedValue(undefined);
}

describe('SshKeysPageComponent', () => {
  let service: FakeSshKeysService;

  // This component (and the nested app-sidebar-toggle → app-icon) carries
  // an external styleUrl, which JIT compilation in jsdom refuses to
  // resolve on its own — see project-list.component.spec.ts.
  beforeAll(async () => {
    await ɵresolveComponentResources(() => Promise.resolve(''));
  });

  beforeEach(async () => {
    service = new FakeSshKeysService();
    await TestBed.configureTestingModule({
      imports: [
        SshKeysPageComponent,
        TranslocoTestingModule.forRoot({
          langs: {en},
          translocoConfig: {availableLangs: ['en'], defaultLang: 'en'},
        }),
      ],
      providers: [
        {provide: SshKeysService, useValue: service},
        {provide: Router, useValue: {navigateByUrl: vi.fn()}},
        // Real SidebarService reads window.matchMedia, which jsdom does not
        // implement — stubbed for the same reason project-list.component.
        // spec.ts stubs it.
        {provide: SidebarService, useValue: {collapsed: signal(false), expand: vi.fn()}},
      ],
    }).compileComponents();
  });

  it('lists the user keys on init', async () => {
    const fixture = TestBed.createComponent(SshKeysPageComponent);
    fixture.detectChanges();
    await fixture.whenStable();
    expect(service.loadKeys).toHaveBeenCalled();
  });

  it('shows the sign command containing the issued challenge', async () => {
    const fixture = TestBed.createComponent(SshKeysPageComponent);
    const component = fixture.componentInstance;
    await component.startRegistration();
    expect(component.signCommand()).toContain('srw-nonce');
    expect(component.signCommand()).toContain('ssh-keygen -Y sign');
    expect(component.signCommand()).toContain('srw-ssh-key-registration');
  });

  it('sends the challenge back with the signature', async () => {
    const fixture = TestBed.createComponent(SshKeysPageComponent);
    const component = fixture.componentInstance;
    await component.startRegistration();
    component.form.name = 'laptop';
    component.form.publicKey = 'ssh-ed25519 AAAA me@host';
    component.form.signature = '-----BEGIN SSH SIGNATURE-----';
    await component.submit();
    expect(service.createKey).toHaveBeenCalledWith(
      expect.objectContaining({challenge: 'srw-nonce'}),
    );
  });

  it('surfaces a duplicate-key conflict rather than swallowing it', async () => {
    service.createKey.mockRejectedValue({status: 409, error: {detail: 'already registered'}});
    const fixture = TestBed.createComponent(SshKeysPageComponent);
    const component = fixture.componentInstance;
    await component.startRegistration();
    component.form.name = 'laptop';
    component.form.publicKey = 'ssh-ed25519 AAAA';
    component.form.signature = 'sig';
    await component.submit();
    expect(component.error()).toContain('already registered');
  });

  it('does not delete without confirmation', async () => {
    const fixture = TestBed.createComponent(SshKeysPageComponent);
    const component = fixture.componentInstance;
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    await component.remove(service.keys[0]);
    expect(service.deleteKey).not.toHaveBeenCalled();
  });
});
