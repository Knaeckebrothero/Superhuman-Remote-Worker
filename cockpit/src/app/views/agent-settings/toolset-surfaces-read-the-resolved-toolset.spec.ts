import {describe, expect, it} from 'vitest';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';

/**
 * Every surface that shows a toolset must ask the server what would be bound.
 *
 * Two of them shipped without doing so. Job create mounted
 * `<app-agent-settings mode="job">` with no `resolvedToolset`, no
 * `readsResolvedToolset` and no `gatedCapabilities`, rendering six hardcoded rows
 * with two states and no grant gating while New Session rendered twenty-five with
 * three. The expert editor mounted `<app-tools-group>` the same way — the worse
 * of the two, because an expert's toolset is what every job and session built
 * from it inherits. Removing the job-create binding again passed all fifteen of
 * that form's tests, which is why this file exists.
 *
 * These are SOURCE assertions, and deliberately crude. Mounting these components
 * needs a dozen service stubs and the repo's own spec header says TestBed is not
 * set up for it; the invariant worth protecting is one line of template per
 * surface, so a scan buys most of the value for none of the setup. It cannot
 * verify the bindings do the right thing at runtime — `tools-group.render.spec.ts`
 * covers that, given an answer — only that each surface still hands one over.
 *
 * If these components ever get real component tests, delete this file rather
 * than keeping both.
 *
 * Known limit, learned the hard way: a scan cannot tell a binding from a
 * TYPO. The expert-editor assertions here passed against
 * `[resolvedToolset]="toolPreview()"` on `app-tools-group`, whose input is
 * actually named `resolved` — `tsc --noEmit` and all 1661 cockpit tests passed
 * too, and only `ng build` (the template compiler) rejected it. So keep the
 * per-host binding names below honest by hand, and treat a green run here as
 * evidence the line EXISTS, never that it binds.
 */

/** Surfaces mounting `app-agent-settings`, which forwards to the tools group. */
const FORMS = {
  'job create': 'src/app/views/create/job-create.component.ts',
  'session create': 'src/app/views/session-create/session-create.component.ts',
} as const;

/** Mounts `app-tools-group` directly, and gates from its own computed. */
const EXPERT_EDITOR = 'src/app/views/experts/expert-editor.component.ts';

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

/**
 * The bindings every host passes.
 *
 * `answerInput` differs by what the host mounts, and the difference is real:
 * `AgentSettingsComponent` takes `resolvedToolset` and forwards it to the tools
 * group's `resolved`, so a surface mounting the group DIRECTLY must bind
 * `resolved`. Binding the wrapper's name on the inner component compiles under
 * `tsc` and passes every unit test; only `ng build` catches it.
 */
function assertReadsTheAnswer(text: string, answerInput: 'resolvedToolset' | 'resolved'): void {
  expect(text).toContain(`[${answerInput}]="toolPreview()"`);
  // Without this the pane cannot distinguish "the read failed" from "nobody
  // asked", and flies a permanent could-not-be-read banner.
  expect(text).toMatch(/\[readsResolvedToolset\]="true"/);
  expect(text).toContain('previewToolGroups(');
}

describe('toolset surfaces read the resolved toolset', () => {
  for (const [label, rel] of Object.entries(FORMS)) {
    describe(label, () => {
      const text = source(rel);

      it('reads the answer and says that it did', () =>
        assertReadsTheAnswer(text, 'resolvedToolset'));

      it('passes the author grants so a blocked row greys instead of lying', () => {
        expect(text).toMatch(/\[gatedCapabilities\]="capabilities\.grants\(\) \?\? null"/);
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

  describe('expert editor', () => {
    const text = source(EXPERT_EDITOR);

    // Mounts the tools group directly, so it binds `resolved`, not the
    // wrapper's `resolvedToolset`.
    it('reads the answer and says that it did', () => assertReadsTheAnswer(text, 'resolved'));

    it('passes the author grants so a blocked row greys instead of lying', () => {
      // Its own computed rather than the service, because null there means
      // "admin, no gating" and the editor already distinguishes that from
      // "still loading".
      expect(text).toMatch(/\[gatedCapabilities\]="gatedCapabilities\(\)"/);
    });

    it('asks for whichever type is being edited, never a hardcoded one', () => {
      // Both bases are reachable from this one form, so a literal here would
      // mispredict for half of all experts. Only the call site is asserted —
      // the payload itself is covered by `expertToolPreviewRequest`'s own unit
      // tests. A "no literal type anywhere" scan looks stronger and is not: it
      // matches the form's own `expert_type: 'worker'` default, which is
      // unrelated and correct.
      expect(text).toContain('expertToolPreviewRequest(this.expertType()');
    });

    it('does not let the weaker config-derived prefill land on top', () => {
      // `prefillFromConfig` infers enablement from config names; on a real
      // session that over-reported by 24 tools. It races the HTTP answer, so
      // the guard is what keeps the better fact from being overwritten.
      expect(text).toMatch(
        /if \(!this\.resolvedAnchorApplied\) this\.toolsGroup\(\)\?\.prefillFromConfig/,
      );
    });
  });
});
