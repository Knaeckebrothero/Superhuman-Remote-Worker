import {describe, expect, it} from 'vitest';
import {
    buildToolCardView,
    clampLine,
    languageFromPath,
    pickArg,
    prettifyToolName,
    resolveToolDescriptor,
} from './tool-descriptors';
import {toolCardViewFromAudit, toolCardViewFromEvent} from './tool-card-adapters';
import {NormalizedToolCall} from '../models/tool-card.model';
import {ToolCallEvent} from '../models/turn.model';
import {AuditEntry} from '../models/audit.model';

function norm(partial: Partial<NormalizedToolCall> & {tool: string}): NormalizedToolCall {
    return {args: {}, status: 'ok', ...partial};
}

describe('helpers', () => {
    it('pickArg returns the first present key, stringifying non-strings', () => {
        expect(pickArg({path: 'a.ts'}, ['file_path', 'path'])).toBe('a.ts');
        expect(pickArg({n: 3}, 'n')).toBe('3');
        expect(pickArg({}, 'missing')).toBe('');
    });

    it('clampLine collapses whitespace and ellipsizes past the cap', () => {
        const long = 'x'.repeat(100);
        const out = clampLine(long, 64);
        expect(out.length).toBeLessThanOrEqual(64);
        expect(out.endsWith('…')).toBe(true);
        expect(clampLine('a\n  b', 64)).toBe('a b');
    });

    it('languageFromPath maps extensions and Dockerfile', () => {
        expect(languageFromPath('src/app/foo.ts')).toBe('typescript');
        expect(languageFromPath('a/b.py')).toBe('python');
        expect(languageFromPath('Dockerfile')).toBe('docker');
        expect(languageFromPath('LICENSE')).toBeUndefined();
    });

    it('prettifyToolName humanizes snake/camel ids', () => {
        expect(prettifyToolName('frobnicate_thing')).toBe('Frobnicate thing');
    });
});

describe('buildToolCardView', () => {
    it('read_file → Path param + code result with language from the extension', () => {
        const v = buildToolCardView(
            norm({tool: 'read_file', args: {path: 'src/app/foo.ts'}, result: 'export const x = 1;\n'}),
        );
        expect(v.title).toBe('Read file');
        expect(v.icon).toBe('description');
        expect(v.subtitle).toBe('foo.ts');
        expect(v.params).toEqual([{label: 'Path', value: 'src/app/foo.ts', kind: 'path'}]);
        expect(v.result?.kind).toBe('code');
        expect(v.result?.language).toBe('typescript');
        expect(v.result?.content).toContain('export const');
        expect(v.details).toEqual([]); // trivial read: no execution details
    });

    it('run_command keeps the FULL chained command and clamps only the hint', () => {
        const cmd = 'cd /very/long/path && ./build.sh --flag --another-flag && echo done now please';
        const v = buildToolCardView(
            norm({tool: 'run_command', args: {command: cmd}, result: 'ok', durationMs: 2300, exitCode: 0}),
        );
        expect(v.title).toBe('Execute command');
        // Full command survives in the body, untruncated …
        expect(v.params[0]).toEqual({label: 'Command', value: cmd, kind: 'code'});
        // … but the collapsed hint is clamped.
        expect(v.subtitle!.length).toBeLessThanOrEqual(64);
        expect(v.subtitle!.endsWith('…')).toBe(true);
        expect(v.result?.kind).toBe('terminal');
        expect(v.details).toContainEqual({label: 'Duration', value: '2.3s'});
        expect(v.details).toContainEqual({label: 'Exit code', value: '0', tone: 'ok'});
    });

    it('edit_file replace → a real old→new diff', () => {
        const v = buildToolCardView(
            norm({tool: 'edit_file', args: {path: 'a.ts', old_string: 'foo', new_string: 'bar'}}),
        );
        expect(v.result?.kind).toBe('diff');
        expect(v.result?.diffMode).toBe('replace');
        expect(v.result?.diffLines).toContainEqual({type: 'del', text: 'foo'});
        expect(v.result?.diffLines).toContainEqual({type: 'add', text: 'bar'});
    });

    it('write_file → all-additions diff', () => {
        const v = buildToolCardView(norm({tool: 'write_file', args: {path: 'a.ts', content: 'l1\nl2'}}));
        expect(v.result?.diffMode).toBe('write');
        expect(v.result?.diffLines?.every((l) => l.type === 'add')).toBe(true);
    });

    it('a failed file-mutation shows the error output, not a phantom diff', () => {
        const v = buildToolCardView(
            norm({tool: 'edit_file', status: 'error', args: {path: 'a.ts', old_string: 'x', new_string: 'y'}, result: 'Permission denied'}),
        );
        expect(v.result?.kind).toBe('text');
        expect(v.result?.content).toBe('Permission denied');
    });

    it('unknown tool falls back to a prettified title + dynamic params', () => {
        const d = resolveToolDescriptor('frobnicate_thing');
        expect(d.dynamicParams).toBe(true);
        const v = buildToolCardView(norm({tool: 'frobnicate_thing', args: {foo: 'bar', n: 3}, result: 'hi'}));
        expect(v.title).toBe('Frobnicate thing');
        expect(v.params).toContainEqual({label: 'Foo', value: 'bar', kind: 'text'});
        expect(v.params).toContainEqual({label: 'N', value: '3', kind: 'text'});
        expect(v.result?.kind).toBe('text');
    });
});

describe('toolCardViewFromEvent', () => {
    const ev = (over: Partial<ToolCallEvent>): ToolCallEvent => ({
        kind: 'tool_call',
        id: 't1',
        tool: 'read_file',
        args: {path: 'x.md'},
        status: 'completed',
        startedAt: 0,
        ...over,
    });

    it('maps completed → ok and derives a markdown language', () => {
        const v = toolCardViewFromEvent(ev({result: '# Hi', durationMs: 12}));
        expect(v.status).toBe('ok');
        expect(v.result?.language).toBe('markdown');
        expect(v.details).toContainEqual({label: 'Duration', value: '12ms'});
    });

    it('passes denied/error statuses through', () => {
        expect(toolCardViewFromEvent(ev({status: 'denied'})).status).toBe('denied');
        const err = toolCardViewFromEvent(ev({tool: 'run_command', args: {command: 'x'}, status: 'error', result: 'boom'}));
        expect(err.status).toBe('error');
        expect(err.result?.content).toBe('boom');
    });
});

describe('toolCardViewFromAudit', () => {
    const entry = (tool: AuditEntry['tool'], over: Partial<AuditEntry> = {}): AuditEntry =>
        ({
            _id: 'a1',
            job_id: 'j1',
            step_number: 1,
            step_type: 'tool',
            node_name: 'tools',
            timestamp: '2026-06-17T00:00:00Z',
            iteration: 1,
            tool,
            ...over,
        }) as AuditEntry;

    it('maps a successful tool step, surfacing the preview size', () => {
        const v = toolCardViewFromAudit(
            entry({name: 'run_command', arguments: {command: 'ls'}, result_preview: 'a\nb', result_size_bytes: 4096, success: true}, {latency_ms: 500, completed_at: '2026-06-17T00:00:01Z'}),
        );
        expect(v.status).toBe('ok');
        expect(v.result?.kind).toBe('terminal');
        // Preview size is surfaced by the card's "preview · N total" note, not a detail row.
        expect(v.result?.bytesTotal).toBe(4096);
        expect(v.details).toContainEqual({label: 'Duration', value: '500ms'});
    });

    it('maps failure and pending states', () => {
        const failed = toolCardViewFromAudit(entry({name: 'read_file', success: false, error: 'nope'}));
        expect(failed.status).toBe('error');
        expect(failed.error).toBe('nope');

        const pending = toolCardViewFromAudit(entry({name: 'read_file'}, {started_at: '2026-06-17T00:00:00Z', completed_at: null}));
        expect(pending.status).toBe('running');
    });
});
