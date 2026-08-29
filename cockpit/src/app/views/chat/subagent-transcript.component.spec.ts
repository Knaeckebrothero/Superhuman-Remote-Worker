import {signal, ɵresolveComponentResources} from '@angular/core';
import {TestBed} from '@angular/core/testing';
import {provideRouter} from '@angular/router';
import {TranslocoService, TranslocoTestingModule} from '@jsverse/transloco';
import {afterEach, beforeAll, describe, expect, it} from 'vitest';
import type {PersistentThreadMessage, Thread} from '../../core/models/api.model';
import en from '../../../assets/i18n/en.json';
import {SubagentTranscriptComponent} from './subagent-transcript.component';

const thread: Thread = {
  id: 'child-1',
  title: 'Subagent tester-7f3a',
  kind: 'subagent',
  status: 'active',
  config_name: 'session_base',
  permission_mode: 'autonomous',
  created_at: '2026-08-29T10:00:00Z',
  last_activity: '2026-08-29T10:05:00Z',
  total_turns: 1,
  total_tokens: 42,
  parent_job_id: 'a2826a91-38a7-4d25-b889-46fabcc93b96',
  subagent_handle: 'tester-7f3a',
  subagent_type: 'tester',
  subagent_status: 'running',
};

const messages: PersistentThreadMessage[] = [
  {
    id: 'm1',
    role: 'human',
    content: 'Run the focused tests.',
    tool_calls: null,
    turn_number: 1,
    created_at: '2026-08-29T10:00:00Z',
  },
  {
    id: 'm2',
    role: 'ai',
    content: 'All focused tests passed.',
    tool_calls: null,
    turn_number: 1,
    created_at: '2026-08-29T10:05:00Z',
  },
];

describe('SubagentTranscriptComponent', () => {
  beforeAll(async () => {
    await ɵresolveComponentResources(() => Promise.resolve(''));
  });
  afterEach(() => TestBed.resetTestingModule());

  function render(over: Partial<Thread> = {}) {
    TestBed.configureTestingModule({
      imports: [
        SubagentTranscriptComponent,
        TranslocoTestingModule.forRoot({
          langs: {en},
          translocoConfig: {availableLangs: ['en'], defaultLang: 'en'},
        }),
      ],
      providers: [provideRouter([])],
    });
    const transloco = TestBed.inject(TranslocoService);
    transloco.setTranslation(en, 'en');
    transloco.setActiveLang('en');
    const fixture = TestBed.createComponent(SubagentTranscriptComponent);
    Object.defineProperty(fixture.componentInstance, 'thread', {
      value: signal({...thread, ...over}),
    });
    Object.defineProperty(fixture.componentInstance, 'messages', {value: signal(messages)});
    Object.defineProperty(fixture.componentInstance, 'loading', {value: signal(false)});
    Object.defineProperty(fixture.componentInstance, 'error', {value: signal(false)});
    fixture.detectChanges();
    return fixture;
  }

  it('renders the child banner, parent link, and durable transcript', () => {
    const root = render().nativeElement as HTMLElement;
    const banner = root.querySelector('[data-testid="subagent-banner"]');

    expect(banner?.textContent).toContain('Subagent transcript · tester-7f3a · tester · Running');
    expect(banner?.textContent).toContain('a2826a91-38a7-4d25-b889-46fabcc93b96');
    expect(banner?.querySelector('a')?.getAttribute('href')).toBe('/jobs');
    expect(root.textContent).toContain('Run the focused tests.');
    expect(root.textContent).toContain('All focused tests passed.');
  });

  it('contains no session mutation, settings, new-session, or composer controls', () => {
    const root = render().nativeElement as HTMLElement;

    expect(root.querySelector('.composer')).toBeNull();
    expect(root.querySelector('.resume-card')).toBeNull();
    expect(root.querySelector('.settings-btn')).toBeNull();
    expect(root.textContent).not.toContain('End session');
    expect(root.textContent).not.toContain('Resume');
    expect(root.textContent).not.toContain('Delete');
    expect(root.textContent).not.toContain('Start a new session');
  });

  it('offers manual refresh while the child is running', () => {
    expect(render().nativeElement.querySelector('.refresh-action')).not.toBeNull();
  });

  it('hides manual refresh after the child finishes', () => {
    expect(
      render({subagent_status: 'completed', status: 'ended'}).nativeElement.querySelector(
        '.refresh-action',
      ),
    ).toBeNull();
  });
});
