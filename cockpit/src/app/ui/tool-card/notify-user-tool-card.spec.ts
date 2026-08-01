import {signal, ɵresolveComponentResources} from '@angular/core';
import {provideHttpClient} from '@angular/common/http';
import {provideHttpClientTesting} from '@angular/common/http/testing';
import {ComponentFixture, TestBed} from '@angular/core/testing';
import {TranslocoTestingModule} from '@jsverse/transloco';
import {provideMarkdown} from 'ngx-markdown';
import {afterEach, beforeAll, beforeEach, describe, expect, it, vi} from 'vitest';
import {NormalizedToolCall, ToolCardView} from '../../core/models/tool-card.model';
import {CanvasService} from '../../core/services/canvas.service';
import {buildToolCardView, parseNotifyMessage} from '../../core/tools/tool-descriptors';
import {AppToolCardComponent} from './tool-card.component';

/**
 * Officer `notify_user` calls render as a first-class chat bubble (the officer
 * is *addressing* the user), not a collapsed tool card: urgency chip + subject
 * + body, with the tool's result string as the delivery-receipt footer. Every
 * other tool keeps the generic `.tc` card. See the `notify` field on
 * ToolCardView and the bubble branch in tool-card.component.ts.
 */

function notifyCall(over: Partial<NormalizedToolCall> = {}): NormalizedToolCall {
  return {
    tool: 'notify_user',
    args: {
      message: 'Job **alpha** failed twice; I paused the loop.',
      urgency: 'page',
      subject: 'Loop halted',
    },
    status: 'ok',
    result: 'Paged the Legate (2/3 pages used today).',
    ...over,
  };
}

describe('parseNotifyMessage', () => {
  it('extracts urgency, subject, body, and the receipt from a call', () => {
    const view = buildToolCardView(notifyCall());
    expect(view.notify).toEqual({
      urgency: 'page',
      subject: 'Loop halted',
      body: 'Job **alpha** failed twice; I paused the loop.',
      receipt: 'Paged the Legate (2/3 pages used today).',
    });
  });

  it('degrades an unknown/absent urgency to log (the tool default), tolerating case', () => {
    const at = (args: Record<string, unknown>) =>
      parseNotifyMessage({tool: 'notify_user', args, status: 'ok'}, args)?.urgency;
    expect(at({message: 'm', urgency: 'PAGE'})).toBe('page');
    expect(at({message: 'm', urgency: 'urgent'})).toBe('log');
    expect(at({message: 'm'})).toBe('log');
  });

  it('omits a blank subject and a not-yet-arrived receipt', () => {
    const n = notifyCall({args: {message: 'm', urgency: 'log', subject: ''}, result: null, status: 'running'});
    expect(parseNotifyMessage(n, n.args)).toEqual({urgency: 'log', body: 'm'});
  });

  it('is absent on every other tool', () => {
    expect(buildToolCardView({tool: 'run_command', args: {command: 'ls'}, status: 'ok'}).notify).toBeUndefined();
    expect(parseNotifyMessage({tool: 'send_message', args: {}, status: 'ok'}, {})).toBeUndefined();
  });
});

describe('notify_user tool-card rendering', () => {
  // AppToolCardComponent (and <app-icon>) use external styleUrls; this
  // project's vitest setup JIT-compiles raw TS, so the pending resource queue
  // must be drained before TestBed can compile them (see
  // memory-panel.component.spec.ts for the pattern).
  beforeAll(async () => {
    await ɵresolveComponentResources(() => Promise.resolve(''));
  });

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [
        AppToolCardComponent,
        TranslocoTestingModule.forRoot({
          langs: {
            en: {
              toolCard: {
                status: {ok: 'completed', running: 'running', pending: 'pending', error: 'error', denied: 'denied'},
                titles: {run_command: 'Execute command'},
                sections: {result: 'Result'},
                notify: {
                  from: 'Officer → you',
                  sending: 'sending…',
                  urgency: {page: 'Page', digest: 'Digest', log: 'Log'},
                },
              },
            },
          },
          translocoConfig: {availableLangs: ['en'], defaultLang: 'en'},
        }),
      ],
      providers: [
        // <markdown> carries ExternalImageDirective (selector: 'markdown'),
        // which injects HttpClient; nothing in these bodies fetches.
        provideHttpClient(),
        provideHttpClientTesting(),
        provideMarkdown(),
        // Only read behind `view().action`, which never exists here.
        {provide: CanvasService, useValue: {state: vi.fn(() => null)}},
      ],
    });
  });

  afterEach(() => TestBed.resetTestingModule());

  /**
   * Render the REAL AppToolCardComponent template for a view. Signal `input()`
   * metadata doesn't compile under this vitest pipeline (ɵcmp.inputs is {} —
   * the gap documented in contact-form.component.spec.ts), so neither template
   * binding nor setInput can reach `view`. Instead the required InputSignal
   * instance field is replaced with a plain signal before the first change
   * detection — the template and every computed only ever call `this.view()`.
   */
  async function render(view: ToolCardView): Promise<{root: HTMLElement; fixture: ComponentFixture<AppToolCardComponent>}> {
    await TestBed.compileComponents();
    const fixture = TestBed.createComponent(AppToolCardComponent);
    (fixture.componentInstance as unknown as {view: () => ToolCardView}).view = signal(view);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return {root: fixture.nativeElement as HTMLElement, fixture};
  }

  it('renders the officer bubble: label, urgency chip, subject, body, receipt footer', async () => {
    const {root} = await render(buildToolCardView(notifyCall()));

    const bubble = root.querySelector('.tc-notify');
    expect(bubble).not.toBeNull();
    // Not a generic collapsed card — the whole point.
    expect(root.querySelector('.tc')).toBeNull();

    // Addressed-to label so it cannot be mistaken for a user message.
    expect(root.querySelector('.tc-notify__from')?.textContent?.trim()).toBe('Officer → you');

    const chip = root.querySelector('.tc-notify__chip');
    expect(chip?.classList.contains('tc-notify__chip--page')).toBe(true);
    expect(chip?.textContent?.trim()).toBe('Page');

    expect(root.querySelector('.tc-notify__subject')?.textContent?.trim()).toBe('Loop halted');

    // The body renders as message markdown, not as a raw-args <pre>.
    const body = root.querySelector('.tc-notify__body');
    expect(body?.textContent).toContain('failed twice; I paused the loop.');
    expect(body?.querySelector('strong')?.textContent).toBe('alpha');

    // The result string is the delivery receipt.
    expect(root.querySelector('.tc-notify__receipt')?.textContent?.trim())
      .toBe('Paged the Legate (2/3 pages used today).');
  });

  it('maps each urgency to its chip class', async () => {
    for (const [urgency, label] of [['page', 'Page'], ['digest', 'Digest'], ['log', 'Log']] as const) {
      const call = notifyCall({args: {message: 'm', urgency}, result: 'Logged.'});
      const {root, fixture} = await render(buildToolCardView(call));
      const chip = root.querySelector('.tc-notify__chip');
      expect(chip?.classList.contains(`tc-notify__chip--${urgency}`)).toBe(true);
      expect(chip?.textContent?.trim()).toBe(label);
      // No subject arg → no subject row.
      expect(root.querySelector('.tc-notify__subject')).toBeNull();
      fixture.destroy();
    }
  });

  it('still renders the generic collapsed card for any other tool (regression)', async () => {
    const {root} = await render(
      buildToolCardView({tool: 'run_command', args: {command: 'ls -la'}, status: 'ok'}),
    );
    expect(root.querySelector('.tc-notify')).toBeNull();
    const card = root.querySelector('details.tc');
    expect(card).not.toBeNull();
    expect(card?.querySelector('.tc__title')?.textContent?.trim()).toBe('Execute command');
  });
});
