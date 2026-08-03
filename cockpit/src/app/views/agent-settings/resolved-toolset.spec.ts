import {describe, expect, it} from 'vitest';
import type {
  SessionToolCategory,
  SessionToolGroupsResponse,
} from '../../core/services/api.service';
import {ALL_TOOL_CATEGORIES, SESSION_TOOL_CATEGORIES} from './agent-settings.types';
import {
  enablePolicy,
  enabledCategoryKeys,
  humanizeCategoryKey,
  isMeasured,
  orderedCategoryKeys,
  resolvedToolRows,
  toolsFragment,
  toolsetProvenance,
} from './resolved-toolset';

/**
 * The view model both settings surfaces read.
 *
 * These are the rules that, if broken, put a wrong answer on screen while
 * every other test still passes — the exact failure this series exists to
 * remove. The contract's six sharp edges are pinned here individually.
 */

function cat(over: Partial<SessionToolCategory> = {}): SessionToolCategory {
  return {
    state: 'on',
    settable: true,
    reason: null,
    decided_by: 'base',
    tools: [],
    ...over,
  };
}

function response(over: Partial<SessionToolGroupsResponse> = {}): SessionToolGroupsResponse {
  return {
    thread_id: 't1',
    source: 'resolved',
    origin: 'agent',
    observed_at: '2026-08-03T10:00:00Z',
    prediction_reason: null,
    degraded_reason: null,
    tool_groups: {},
    categories: {},
    ...over,
  };
}

describe('toolsetProvenance', () => {
  it('reads a full measurement', () => {
    const p = toolsetProvenance(response({categories: {canvas: cat()}}));
    expect(p.trust).toBe('measured');
    expect(p.observedAt).toBe('2026-08-03T10:00:00Z');
    expect(p.detail).toBeNull();
    expect(isMeasured(p.trust)).toBe(true);
  });

  it('SHARP EDGE 1: agent_partial is a measurement, not a forecast', () => {
    // The common path against the currently deployed fleet image: the pod
    // answered, but from /status, so there is no timestamp. A reader that
    // inferred measured-ness from `observed_at` would call this a prediction
    // and tell the user their live session is a guess.
    const p = toolsetProvenance(
      response({
        origin: 'agent_partial',
        observed_at: null,
        degraded_reason: 'this agent image predates GET /session/toolset',
        categories: {canvas: cat()},
      }),
    );
    expect(p.trust).toBe('measured_partial');
    expect(isMeasured(p.trust)).toBe(true);
    expect(p.observedAt).toBeNull();
  });

  it('SHARP EDGE 2: on agent_partial the sentence comes from degraded_reason', () => {
    // observed_at xor prediction_reason does NOT hold here — both are null.
    const p = toolsetProvenance(
      response({
        origin: 'agent_partial',
        observed_at: null,
        prediction_reason: null,
        degraded_reason: 'names only',
        categories: {canvas: cat()},
      }),
    );
    expect(p.detail).toBe('names only');
  });

  it('reads a prediction and carries its reason', () => {
    const p = toolsetProvenance(
      response({
        origin: 'prediction',
        observed_at: null,
        prediction_reason: 'no agent exists for an unsaved session',
        categories: {canvas: cat()},
      }),
    );
    expect(p.trust).toBe('predicted');
    expect(isMeasured(p.trust)).toBe(false);
    expect(p.detail).toBe('no agent exists for an unsaved session');
  });

  it('no response, or no categories, is unknown — never a silent "measured"', () => {
    expect(toolsetProvenance(null).trust).toBe('unknown');
    expect(toolsetProvenance(response({categories: null})).trust).toBe('unknown');
  });

  it('an answer with no origin degrades to predicted, never to measured', () => {
    // Forwards compatibility runs one way only: claiming a measurement we
    // cannot substantiate is the one error that matters.
    const p = toolsetProvenance(response({origin: undefined, categories: {canvas: cat()}}));
    expect(p.trust).toBe('predicted');
  });
});

describe('orderedCategoryKeys', () => {
  it('SHARP EDGE 5: unknown keys are ordered in, not dropped', () => {
    const keys = orderedCategoryKeys(
      {unclassified: cat(), canvas: cat(), some_future_thing: cat(), research: cat()},
      SESSION_TOOL_CATEGORIES,
    );
    // Known keys keep the familiar order; the rest sort alphabetically after.
    expect(keys).toEqual(['research', 'canvas', 'some_future_thing', 'unclassified']);
  });

  it('a metadata entry the answer does not mention is not invented', () => {
    expect(orderedCategoryKeys({canvas: cat()}, SESSION_TOOL_CATEGORIES)).toEqual(['canvas']);
  });
});

describe('resolvedToolRows', () => {
  it('SHARP EDGE 3: read `settable`, do not infer it from `state`', () => {
    // The two are orthogonal, in both directions: a category can be `on` and
    // unsettable (runtime-granted), and `off` is a promise the server only
    // makes when it can keep it. Neither may be reconstructed from the other.
    const rows = resolvedToolRows(
      {
        product_help: cat({state: 'on', settable: false, reason: 'granted by the runtime', tools: ['read_product_guide']}),
        knowledge: cat({state: 'unavailable', settable: false, reason: 'the agent bound none', tools: []}),
      },
      ALL_TOOL_CATEGORIES,
      new Set(),
    );
    const byKey = Object.fromEntries(rows.map((r) => [r.key, r]));
    expect(byKey['product_help'].settable).toBe(false);
    expect(byKey['product_help'].state).toBe('on');
    expect(byKey['knowledge'].settable).toBe(false);
    expect(byKey['knowledge'].state).toBe('unavailable');
  });

  it('SHARP EDGE 4: a bound-but-locked category stays ON, reason and all', () => {
    // The regression this replaces: `state` was derived from `settable` first,
    // so `on` + `settable: false` rendered as `unavailable` — a block glyph
    // over tools the agent is actively holding, with the "you cannot change
    // this" sentence recoloured into "this is off". `product_help` and
    // `session_task` are unconditional persistent-session floors, so this hit
    // EVERY session, and every connector category joins them the moment a
    // datasource is attached.
    const [row] = resolvedToolRows(
      {
        product_help: cat({
          state: 'on',
          settable: false,
          reason: 'granted by the runtime, not by config',
          tools: ['read_product_guide'],
        }),
      },
      ALL_TOOL_CATEGORIES,
      new Set(),
    );
    expect(row.state).toBe('on');
    expect(row.settable).toBe(false);
    expect(row.reason).toBe('granted by the runtime, not by config');
  });

  it('a locked row keeps the server state even when the user set is empty', () => {
    const rows = resolvedToolRows(
      {
        session_task: cat({state: 'on', settable: false, reason: 'runtime', tools: ['task_add']}),
        knowledge: cat({state: 'unavailable', settable: false, reason: 'bound none'}),
      },
      ALL_TOOL_CATEGORIES,
      new Set(['session_task', 'knowledge']),
    );
    const byKey = Object.fromEntries(rows.map((r) => [r.key, r.state]));
    expect(byKey['session_task']).toBe('on');
    expect(byKey['knowledge']).toBe('unavailable');
  });

  it('a user switch flips a settable row, and the row stops being pristine', () => {
    const [row] = resolvedToolRows({canvas: cat({state: 'on'})}, ALL_TOOL_CATEGORIES, new Set(['canvas']));
    expect(row.state).toBe('off');
    expect(row.pristine).toBe(false);
  });

  it('a click can never paint over an `unavailable`', () => {
    // Letting the disabled set win here would rebuild the fail-open: the box
    // would read "on" for a category the agent cannot hold.
    const [row] = resolvedToolRows(
      {shell: cat({state: 'unavailable', settable: false, reason: 'requires the shell_tools capability grant'})},
      ALL_TOOL_CATEGORIES,
      new Set(),
    );
    expect(row.state).toBe('unavailable');
    expect(row.pristine).toBe(true);
  });

  it('an unknown key renders with no metadata rather than being skipped', () => {
    const [row] = resolvedToolRows({some_future_thing: cat()}, ALL_TOOL_CATEGORIES, new Set());
    expect(row.key).toBe('some_future_thing');
    expect(row.meta).toBeNull();
  });

  it('carries the tool count so the row can say how much is in it', () => {
    const [row] = resolvedToolRows(
      {research: cat({tools: ['web_search', 'research_topic']})},
      ALL_TOOL_CATEGORIES,
      new Set(),
    );
    expect(row.toolCount).toBe(2);
  });
});

describe('enabledCategoryKeys', () => {
  it('only `on` counts — `unavailable` is not enabled even when config asked', () => {
    const on = enabledCategoryKeys({
      canvas: cat({state: 'on'}),
      knowledge: cat({state: 'unavailable', settable: false, reason: 'bound none'}),
      git: cat({state: 'off'}),
    });
    expect([...on]).toEqual(['canvas']);
  });
});

describe('enablePolicy', () => {
  it('is `true` for a category the boundary can expand from the registry', () => {
    expect(enablePolicy('orchestrator', {shell: ['run_command']})).toBe(true);
  });

  it('is the served enumeration for a category that refuses `true`', () => {
    expect(enablePolicy('shell', {shell: ['run_command', 'shell_read']})).toEqual({
      only: ['run_command', 'shell_read'],
    });
  });

  it('copies the enumeration rather than aliasing the response', () => {
    const served = {shell: ['run_command']};
    const policy = enablePolicy('shell', served) as {only: string[]};
    policy.only.push('rm');
    expect(served.shell).toEqual(['run_command']);
  });

  it('is null when the category cannot be enabled at all', () => {
    expect(enablePolicy('sql', {sql: []})).toBeNull();
  });

  it('falls back to `true` when the server did not say — a 400 beats a silent drop', () => {
    // An orchestrator older than this contract serves no `enumerate_only`.
    // `true` for shell is then refused at the boundary with a message naming
    // the rule, which is the correct failure for a request it will not honour.
    expect(enablePolicy('shell', null)).toBe(true);
    expect(enablePolicy('shell', undefined)).toBe(true);
  });
});

describe('toolsFragment', () => {
  const base = {
    disabled: new Set<string>(),
    baselineOn: new Set<string>(),
    unsettable: new Set<string>(),
    enumerateOnly: {shell: ['run_command', 'shell_read']} as Record<string, string[]>,
  };

  it('emits nothing when nothing moved', () => {
    // At anchor time the two sets are exact complements over the rendered
    // keys: canvas on, git off, and the user has touched neither.
    expect(
      toolsFragment(['canvas', 'git'], {
        ...base,
        baselineOn: new Set(['canvas']),
        disabled: new Set(['git']),
      }),
    ).toEqual({});
  });

  it('turning a category off emits the empty list', () => {
    expect(
      toolsFragment(['canvas'], {
        ...base,
        baselineOn: new Set(['canvas']),
        disabled: new Set(['canvas']),
      }),
    ).toEqual({canvas: []});
  });

  it('turning a category on emits a POLICY, not names from the layer being overridden', () => {
    // The whole defect: the old code copied `defaultsTools()[cat]`, which is
    // `[]` in both bases for every category worth re-enabling, so the branch
    // emitted nothing at all and the tick did nothing.
    expect(toolsFragment(['orchestrator'], base)).toEqual({orchestrator: true});
  });

  it('turning shell on emits the served enumeration', () => {
    expect(toolsFragment(['shell'], base)).toEqual({
      shell: {only: ['run_command', 'shell_read']},
    });
  });

  it('an unsettable category is never emitted, in either direction', () => {
    expect(
      toolsFragment(['shell'], {...base, unsettable: new Set(['shell'])}),
    ).toEqual({});
    expect(
      toolsFragment(['shell'], {
        ...base,
        baselineOn: new Set(['shell']),
        disabled: new Set(['shell']),
        unsettable: new Set(['shell']),
      }),
    ).toEqual({});
  });

  it('`lockedOn` is per-key: an ON attempt on a category it does not name stays refused', () => {
    // Both categories are unsettable, baseline OFF, and the caller wants both
    // ON — but only `shell` is in `lockedOn`. `sql` must stay blocked even
    // though the exact same shape of write succeeds for `shell` right next to
    // it, proving this is a per-key lookup and not a switch that, once one
    // category qualifies, opens the on-direction for every unsettable key.
    expect(
      toolsFragment(['shell', 'sql'], {
        ...base,
        unsettable: new Set(['shell', 'sql']),
        lockedOn: new Set(['shell']),
      }),
    ).toEqual({shell: {only: ['run_command', 'shell_read']}});
  });

  it('B2 fast-follow: `lockedOn` drops the special-casing entirely — both directions run the ordinary diff', () => {
    // The regression this fixes: B2 made `settable: false` symmetric in
    // toolsFragment, so a category the agent holds via a per-tool code grant
    // (state 'on') could no longer be turned on either — the only sessions
    // locked out of GAINING shell were the ones that did not have it yet.
    //
    // This function does NOT itself refuse the off-direction for a `lockedOn`
    // key — it has no notion of "reaches the wire" versus "local bookkeeping",
    // and the mechanism needs the off-attempt to register SOMEWHERE so a
    // later re-tick can be seen as a change (see the function doc and
    // settings-pane.component.ts::applyChanges, which is the caller that
    // actually enforces "never dispatch the off-write" by stripping it after
    // calling this). Pinned here as the ordinary diff it now is.
    const lockedOn = new Set(['shell']);

    // Baseline on, still on: no local change -> nothing, same as any settable
    // category.
    expect(
      toolsFragment(['shell'], {
        ...base,
        baselineOn: new Set(['shell']),
        unsettable: new Set(['shell']),
        lockedOn,
      }),
    ).toEqual({});

    // Baseline on, user disables it: `lockedOn` lets this emit — the empty
    // list is real LOCAL tracking, not yet a promise to the server.
    expect(
      toolsFragment(['shell'], {
        ...base,
        baselineOn: new Set(['shell']),
        disabled: new Set(['shell']),
        unsettable: new Set(['shell']),
        lockedOn,
      }),
    ).toEqual({shell: []});

    // Baseline OFF (e.g. the caller's own bookkeeping moved it there — see
    // settings-pane.component.ts's `previous`), user wants it on: ON is a real
    // write, because this is how the config-grantable subset actually gets
    // added alongside the code-granted tools.
    expect(
      toolsFragment(['shell'], {
        ...base,
        baselineOn: new Set(),
        unsettable: new Set(['shell']),
        lockedOn,
      }),
    ).toEqual({shell: {only: ['run_command', 'shell_read']}});
  });

  it('a category that cannot be enabled emits nothing rather than a doomed request', () => {
    expect(toolsFragment(['sql'], {...base, enumerateOnly: {sql: []}})).toEqual({});
  });
});

describe('humanizeCategoryKey', () => {
  it('beats rendering a raw transloco key on screen', () => {
    expect(humanizeCategoryKey('browser_direct')).toBe('Browser Direct');
    expect(humanizeCategoryKey('mcp')).toBe('Mcp');
  });
});
