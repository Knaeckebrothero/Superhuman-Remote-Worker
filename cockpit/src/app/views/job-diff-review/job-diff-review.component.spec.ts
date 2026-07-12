import { describe, it, expect } from 'vitest';
import { diffApiFor, isBinaryEntry } from './job-diff-review.component';

/**
 * Unit tests for the pure helpers used by JobDiffReviewComponent.
 *
 * Mirrors the pattern in JobListComponent — TestBed-free, focusing on
 * extension-to-language inference and status mapping. Full component
 * behavior is exercised via the cluster Playwright smoke run when
 * Slice 3b is ready to verify.
 *
 * `diffApiFor` / `isBinaryEntry` are imported (not duplicated) from the
 * component — Task 14's thread-mode generalization — so this spec exercises
 * the real implementation the component uses.
 */

function languageFromPath(path: string): string {
  const ext = path.split('.').pop()?.toLowerCase() ?? '';
  const map: Record<string, string> = {
    md: 'markdown',
    markdown: 'markdown',
    json: 'json',
    yaml: 'yaml',
    yml: 'yaml',
    ts: 'typescript',
    tsx: 'typescript',
    js: 'javascript',
    jsx: 'javascript',
    py: 'python',
    sh: 'shell',
    bash: 'shell',
    sql: 'sql',
    html: 'html',
    css: 'css',
    scss: 'scss',
    xml: 'xml',
    txt: 'plaintext',
  };
  return map[ext] ?? 'plaintext';
}

function statusGlyph(status: 'added' | 'modified' | 'deleted'): string {
  return status === 'added' ? '+' : status === 'deleted' ? '−' : 'M';
}

describe('JobDiffReviewComponent helpers', () => {
  describe('languageFromPath', () => {
    it('infers markdown from .md', () => {
      expect(languageFromPath('projects/foo/README.md')).toBe('markdown');
    });

    it('infers yaml from .yml and .yaml', () => {
      expect(languageFromPath('projects/foo/config.yaml')).toBe('yaml');
      expect(languageFromPath('projects/foo/.gitlab-ci.yml')).toBe('yaml');
    });

    it('infers typescript from .ts and .tsx', () => {
      expect(languageFromPath('projects/foo/index.ts')).toBe('typescript');
      expect(languageFromPath('projects/foo/component.tsx')).toBe('typescript');
    });

    it('infers python from .py', () => {
      expect(languageFromPath('projects/foo/script.py')).toBe('python');
    });

    it('falls back to plaintext for unknown extensions', () => {
      expect(languageFromPath('projects/foo/notes.xyz')).toBe('plaintext');
    });

    it('handles paths without an extension', () => {
      expect(languageFromPath('projects/foo/Dockerfile')).toBe('plaintext');
    });

    it('is case-insensitive', () => {
      expect(languageFromPath('projects/foo/README.MD')).toBe('markdown');
      expect(languageFromPath('projects/foo/Config.JSON')).toBe('json');
    });
  });

  describe('statusGlyph', () => {
    it('returns + for added', () => {
      expect(statusGlyph('added')).toBe('+');
    });

    it('returns − (U+2212 minus sign, not ASCII -) for deleted', () => {
      expect(statusGlyph('deleted')).toBe('−');
      expect(statusGlyph('deleted')).not.toBe('-');
    });

    it('returns M for modified', () => {
      expect(statusGlyph('modified')).toBe('M');
    });
  });
});

describe('diffApiFor', () => {
  it('returns "job" when only jobId is set', () => {
    expect(diffApiFor('job-1', null)).toBe('job');
  });

  it('returns "thread" when only threadId is set', () => {
    expect(diffApiFor(null, 'thread-1')).toBe('thread');
  });

  it('throws when both jobId and threadId are set (host wiring bug)', () => {
    expect(() => diffApiFor('job-1', 'thread-1')).toThrow();
  });

  it('throws when neither jobId nor threadId is set (host wiring bug)', () => {
    expect(() => diffApiFor(null, null)).toThrow();
  });
});

describe('isBinaryEntry', () => {
  it('is true when the file-tree entry is flagged binary', () => {
    expect(isBinaryEntry({ binary: true }, null)).toBe(true);
  });

  it('is true when the loaded file reports old_binary', () => {
    expect(isBinaryEntry({}, { old_binary: true, new_binary: false })).toBe(true);
  });

  it('is true when the loaded file reports new_binary', () => {
    expect(isBinaryEntry({}, { old_binary: false, new_binary: true })).toBe(true);
  });

  it('is false for a plain-text entry with no file loaded yet', () => {
    expect(isBinaryEntry({}, null)).toBe(false);
  });

  it('is false when neither the entry nor the loaded file are flagged binary', () => {
    expect(
      isBinaryEntry({ binary: false }, { old_binary: false, new_binary: false }),
    ).toBe(false);
  });
});
