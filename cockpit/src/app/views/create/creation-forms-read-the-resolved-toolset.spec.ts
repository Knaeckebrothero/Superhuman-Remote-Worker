import {describe, expect, it} from 'vitest';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';

/**
 * Both creation forms must ask the server what the agent would bind.
 *
 * Job create shipped without doing so: it mounted `<app-agent-settings mode="job">`
 * with no `resolvedToolset`, no `readsResolvedToolset` and no `gatedCapabilities`,
 * so it rendered six hardcoded rows with two states and no grant gating while the
 * New Session form rendered twenty-five with three. Removing the binding again
 * passed all fifteen job-create tests, which is why this file exists.
 *
 * This is a SOURCE assertion, and deliberately crude. Mounting JobCreateComponent
 * needs a dozen service stubs and the repo's own spec header says TestBed is not
 * set up for it; the invariant worth protecting is one line of template per form,
 * so a scan buys most of the value for none of the setup. It cannot verify the
 * bindings do the right thing at runtime — `tools-group.render.spec.ts` covers
 * that, given an answer — only that each form still hands one over.
 *
 * If these forms ever get real component tests, delete this file rather than
 * keeping both.
 */

const FORMS = {
  'job create': 'src/app/views/create/job-create.component.ts',
  'session create': 'src/app/views/session-create/session-create.component.ts',
} as const;

/**
 * Source with comments removed.
 *
 * Not incidental. The first version of this file matched the raw text, and the
 * `expert_type: 'worker'` assertion passed with the call site DELETED, because
 * the method's own docstring quotes the argument it sends. A source-scanning
 * test that reads its own documentation as evidence is worse than no test.
 */
function source(rel: string): string {
  return readFileSync(resolve(process.cwd(), rel), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '');
}

describe('creation forms read the resolved toolset', () => {
  for (const [label, rel] of Object.entries(FORMS)) {
    describe(label, () => {
      const text = source(rel);

      it('passes the server answer to the settings component', () => {
        expect(text).toMatch(/\[resolvedToolset\]="toolPreview\(\)"/);
      });

      it('declares that it performs the read, so a failure reads as a failure', () => {
        // Without this the pane cannot distinguish "the read failed" from
        // "nobody asked", and flies a permanent could-not-be-read banner.
        expect(text).toMatch(/\[readsResolvedToolset\]="true"/);
      });

      it('passes the author grants so a blocked row greys instead of lying', () => {
        expect(text).toMatch(/\[gatedCapabilities\]="capabilities\.grants\(\) \?\? null"/);
      });

      it('calls previewToolGroups', () => {
        expect(text).toContain('previewToolGroups(');
      });
    });
  }

  it('job create asks for a WORKER prediction, not a session one', () => {
    // The endpoint defaults to session, so an omitted expert_type silently
    // predicts session_base — a different toolset from the job that will run.
    expect(source(FORMS['job create'])).toMatch(/expert_type:\s*'worker'/);
  });

  it('session create does not ask for a worker prediction', () => {
    expect(source(FORMS['session create'])).not.toMatch(/expert_type:\s*'worker'/);
  });
});
