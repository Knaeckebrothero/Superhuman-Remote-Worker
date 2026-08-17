import {signal, ɵresolveComponentResources} from '@angular/core';
import {provideHttpClient} from '@angular/common/http';
import {provideHttpClientTesting} from '@angular/common/http/testing';
import {ComponentFixture, TestBed} from '@angular/core/testing';
import {TranslocoTestingModule} from '@jsverse/transloco';
import {provideMarkdown} from 'ngx-markdown';
import {afterEach, beforeAll, beforeEach, describe, expect, it, vi} from 'vitest';
import {ToolCardView} from '../../core/models/tool-card.model';
import {CanvasService} from '../../core/services/canvas.service';
import {buildToolCardView} from '../../core/tools/tool-descriptors';
import {AppToolCardComponent} from './tool-card.component';

/**
 * The result header's copy button is an `<app-icon-button>` with a REQUIRED
 * signal input (`ariaLabel`, icon-button.component.ts:50). This vitest pipeline
 * drops signal-input metadata — the gap notify-user-tool-card.spec.ts documents
 * and sidesteps by never rendering a result section — so the binding never
 * lands and reading it throws NG0950, killing the whole render.
 *
 * TestBed.overrideComponent can't help: it demands an already-resolved def, and
 * `styleUrl` resolution does not survive resetTestingModule(). Replacing the
 * module before Angular ever loads it does. Decorator inputs still bind fine.
 * Nothing here asserts on the copy button's own markup.
 */
vi.mock('../icon-button', () => import('./icon-button.stub'));

/**
 * A `read_file` on a markdown file renders as prose, with a Rendered|Raw
 * toggle back to the exact bytes the model saw — line numbers included.
 * See knowledge-base/knowledge/superpowers/specs/2026-08-03-tool-card-markdown-rendering-design.md.
 */

/** `read_file` output shape: `f"{i:6}\t{line}"` (src/tools/workspace/files.py:502). */
function numbered(lines: string[]): string {
  return lines.map((line, i) => `${String(i + 1).padStart(6, ' ')}\t${line}`).join('\n');
}

const SKILL_MD = numbered([
  '---',
  'name: cite-as-you-write',
  '---',
  '',
  '# Cite As You Write',
  '',
  'Use whenever you state a **fact**.',
]);

function mdView(content = SKILL_MD, path = 'skills/cite-as-you-write/SKILL.md'): ToolCardView {
  return buildToolCardView({tool: 'read_file', args: {path}, status: 'ok', result: content});
}

describe('markdown tool-card rendering', () => {
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
                titles: {read_file: 'Read file'},
                sections: {result: 'Result'},
                view: {rendered: 'Rendered', raw: 'Raw'},
              },
            },
          },
          translocoConfig: {availableLangs: ['en'], defaultLang: 'en'},
        }),
      ],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideMarkdown(),
        {provide: CanvasService, useValue: {state: vi.fn(() => null)}},
      ],
    });
  });

  afterEach(() => TestBed.resetTestingModule());

  /** Renders the real card; signal inputs don't compile here — see notify-user-tool-card.spec.ts. */
  async function render(view: ToolCardView): Promise<{root: HTMLElement; fixture: ComponentFixture<AppToolCardComponent>}> {
    await TestBed.compileComponents();
    const fixture = TestBed.createComponent(AppToolCardComponent);
    (fixture.componentInstance as unknown as {view: () => ToolCardView}).view = signal(view);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return {root: fixture.nativeElement as HTMLElement, fixture};
  }

  it('renders a markdown file as prose, not as a numbered <pre>', async () => {
    const {root} = await render(mdView());

    const md = root.querySelector('.tc__md');
    expect(md).not.toBeNull();
    expect(root.querySelector('.tc__result')).toBeNull();

    expect(md?.querySelector('h1')?.textContent?.trim()).toBe('Cite As You Write');
    expect(md?.querySelector('strong')?.textContent).toBe('fact');
    // The cat -n prefixes are gone from the rendered text.
    expect(md?.textContent).not.toContain('\t');
    expect(md?.textContent).toContain('Use whenever you state a fact.');
  });

  it('shows frontmatter as a code block rather than a heading', async () => {
    const {root} = await render(mdView());

    const code = root.querySelector('.tc__md pre code');
    expect(code?.textContent).toContain('name: cite-as-you-write');
    // The closing --- must not turn the metadata into a setext H2.
    const headings = [...root.querySelectorAll('.tc__md h1, .tc__md h2')];
    expect(headings.map((h) => h.textContent?.trim())).toEqual(['Cite As You Write']);
  });

  it('flips to the exact numbered source and back', async () => {
    const {root, fixture} = await render(mdView());

    const raw = root.querySelector<HTMLButtonElement>('.tc__view-raw');
    expect(raw).not.toBeNull();
    raw!.click();
    fixture.detectChanges();

    const pre = root.querySelector('.tc__result');
    expect(pre?.textContent).toBe(SKILL_MD);
    expect(root.querySelector('.tc__md')).toBeNull();

    root.querySelector<HTMLButtonElement>('.tc__view-rendered')!.click();
    fixture.detectChanges();
    expect(root.querySelector('.tc__md')).not.toBeNull();
  });

  it('offers no toggle for a non-markdown result', async () => {
    const {root} = await render(
      buildToolCardView({tool: 'read_file', args: {path: 'a/b.py'}, status: 'ok', result: 'x = 1\n'}),
    );

    expect(root.querySelector('.tc__view')).toBeNull();
    expect(root.querySelector('.tc__result')).not.toBeNull();
  });

  it('copies the cleaned source when rendered and the raw bytes when raw', async () => {
    const {root, fixture} = await render(mdView());
    const card = fixture.componentInstance as unknown as {
      copyPayload: (r: NonNullable<ToolCardView['result']>) => string;
    };
    const result = mdView().result!;

    expect(card.copyPayload(result)).toContain('# Cite As You Write');
    expect(card.copyPayload(result)).not.toContain('\t');

    root.querySelector<HTMLButtonElement>('.tc__view-raw')!.click();
    fixture.detectChanges();
    expect(card.copyPayload(result)).toBe(SKILL_MD);
  });
});
