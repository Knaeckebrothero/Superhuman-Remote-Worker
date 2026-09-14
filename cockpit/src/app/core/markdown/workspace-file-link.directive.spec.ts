import {Component} from '@angular/core';
import {TestBed} from '@angular/core/testing';
import {
  MARKED_EXTENSIONS,
  SANITIZE,
  MarkdownComponent,
  provideMarkdown,
} from 'ngx-markdown';
import {afterEach, beforeEach, describe, expect, it} from 'vitest';
import {markdownLinkExtension} from './link-extension';
import {sanitizeMarkdownHtml} from './markdown-sanitizer';
import {WorkspaceFileLinkDirective} from './workspace-file-link.directive';

@Component({
  standalone: true,
  imports: [MarkdownComponent, WorkspaceFileLinkDirective],
  template: `<markdown
    appWorkspaceFileLink
    [data]="content"
    (workspaceFileOpen)="opened.push($event)"
  ></markdown>`,
})
class WorkspaceFileLinkHost {
  content = '';
  readonly opened: string[] = [];
}

async function renderHost(content: string) {
  const fixture = TestBed.createComponent(WorkspaceFileLinkHost);
  fixture.componentInstance.content = content;
  fixture.detectChanges();
  await fixture.whenStable();
  await new Promise((resolve) => setTimeout(resolve, 0));
  fixture.detectChanges();
  return fixture;
}

describe('WorkspaceFileLinkDirective', () => {
  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [WorkspaceFileLinkHost],
      providers: [
        provideMarkdown({
          markedExtensions: [
            {
              provide: MARKED_EXTENSIONS,
              multi: true,
              useValue: markdownLinkExtension(),
            },
          ],
          sanitize: {
            provide: SANITIZE,
            useValue: sanitizeMarkdownHtml,
          },
        }),
      ],
    });
  });

  afterEach(() => TestBed.resetTestingModule());

  it('reports the normalized path instead of navigating the document', async () => {
    const fixture = await renderHost('- [Master prospect CSV](/output/prospects.csv)');
    const root = fixture.nativeElement as HTMLElement;

    const control = root.querySelector<HTMLButtonElement>('button.workspace-file-link');
    expect(control, root.innerHTML).not.toBeNull();
    expect(control?.getAttribute('href')).toBeNull();

    control?.click();
    expect(fixture.componentInstance.opened).toEqual(['output/prospects.csv']);

    fixture.destroy();
  });

  it('emits once per activation, from anywhere inside the control', async () => {
    const fixture = await renderHost('[**Ranked** report](output/report.md)');
    const root = fixture.nativeElement as HTMLElement;

    root.querySelector<HTMLElement>('button.workspace-file-link strong')?.click();

    expect(fixture.componentInstance.opened).toEqual(['output/report.md']);
    fixture.destroy();
  });

  it('stays quiet for ordinary prose and external links', async () => {
    const fixture = await renderHost(
      'See [the site](https://example.test/a) — plain text follows.',
    );
    const root = fixture.nativeElement as HTMLElement;

    root.querySelector<HTMLElement>('a')?.click();
    root.querySelector<HTMLElement>('p')?.click();

    expect(fixture.componentInstance.opened).toEqual([]);
    fixture.destroy();
  });
});
