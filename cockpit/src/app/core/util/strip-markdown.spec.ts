import {describe, expect, it} from 'vitest';
import {stripMarkdown} from './strip-markdown';

describe('stripMarkdown', () => {
    it('returns empty string for nullish input', () => {
        expect(stripMarkdown(undefined)).toBe('');
        expect(stripMarkdown(null)).toBe('');
        expect(stripMarkdown('')).toBe('');
    });

    it('strips leading heading markers', () => {
        expect(stripMarkdown('# Phase 1 Summary')).toBe('Phase 1 Summary');
        expect(stripMarkdown('## Primary Sources')).toBe('Primary Sources');
    });

    it('unwraps inline code and bold', () => {
        expect(stripMarkdown('A `hello` line in `logfmt` format')).toBe('A hello line in logfmt format');
        expect(stripMarkdown('**Keys**: Alphanumeric')).toBe('Keys: Alphanumeric');
    });

    it('keeps link text, drops the URL', () => {
        expect(stripMarkdown('[Brandur: logfmt](https://brandur.org/logfmt)')).toBe('Brandur: logfmt');
    });

    it('keeps image alt text, drops the URL', () => {
        expect(stripMarkdown('![diagram](https://x/y.png) caption')).toBe('diagram caption');
    });

    it('does not mangle snake_case identifiers or file paths', () => {
        expect(stripMarkdown('see output/verification_plan.md and snake_case_name')).toBe(
            'see output/verification_plan.md and snake_case_name',
        );
    });

    it('still unwraps real underscore emphasis at word boundaries', () => {
        expect(stripMarkdown('a _word_ here')).toBe('a word here');
        expect(stripMarkdown('a __bold__ word')).toBe('a bold word');
    });

    it('collapses a multi-line markdown block into one prose line', () => {
        const md = '# Logfmt Syntax Rules\n\n## Syntax Rules\n- **Keys**: Alphanumeric, can include `.`, `_`, and `-`.';
        expect(stripMarkdown(md)).toBe('Logfmt Syntax Rules Syntax Rules Keys: Alphanumeric, can include ., _, and -.');
    });

    it('drops list and blockquote markers', () => {
        expect(stripMarkdown('- item one\n- item two')).toBe('item one item two');
        expect(stripMarkdown('> a quote')).toBe('a quote');
        expect(stripMarkdown('1. first\n2. second')).toBe('first second');
    });
});
