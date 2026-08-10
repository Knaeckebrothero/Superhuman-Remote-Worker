import {describe, expect, it} from 'vitest';
import {
    classifyUploadFailure,
    composeAgentContent,
    uploadSummary,
    type PendingUpload,
} from './upload-stage';

function pending(over: Partial<PendingUpload> = {}): PendingUpload {
    return {
        id: 'p1',
        file: new File(['x'], 'a.pdf'),
        name: 'a.pdf',
        size: 1,
        mimeType: 'application/pdf',
        loaded: 0,
        total: 1,
        status: 'queued',
        ...over,
    };
}

describe('composeAgentContent', () => {
    it('appends the uploads hint after the typed text', () => {
        expect(composeAgentContent('look at this', ['a.pdf', 'b.pdf'])).toBe(
            'look at this\n\n[Attached files in uploads/: a.pdf, b.pdf]',
        );
    });

    it('sends the hint alone when there is no typed text', () => {
        expect(composeAgentContent('', ['a.pdf'])).toBe('[Attached files in uploads/: a.pdf]');
    });

    it('returns the text unchanged when there are no files', () => {
        expect(composeAgentContent('hello', [])).toBe('hello');
    });
});

describe('classifyUploadFailure', () => {
    it('treats size and count rejections as terminal', () => {
        expect(classifyUploadFailure(413)).toBe('terminal');
        expect(classifyUploadFailure(400)).toBe('terminal');
    });

    it('treats a not-ready workspace as retryable', () => {
        expect(classifyUploadFailure(409)).toBe('retryable');
    });

    it('treats transport failures as retryable', () => {
        expect(classifyUploadFailure(0)).toBe('retryable');
        expect(classifyUploadFailure(502)).toBe('retryable');
        expect(classifyUploadFailure(503)).toBe('retryable');
    });
});

describe('uploadSummary', () => {
    it('counts resolved files and reports not-all-done', () => {
        const s = uploadSummary([
            pending({id: 'a', status: 'done'}),
            pending({id: 'b', status: 'uploading'}),
        ]);
        expect(s).toMatchObject({done: 1, total: 2, allDone: false});
        expect(s.firstFailed).toBeUndefined();
    });

    it('reports allDone when every file resolved', () => {
        const s = uploadSummary([pending({id: 'a', status: 'done'})]);
        expect(s.allDone).toBe(true);
    });

    it('surfaces the first failed file', () => {
        const s = uploadSummary([
            pending({id: 'a', status: 'done'}),
            pending({id: 'b', status: 'failed', error: 'File too large'}),
        ]);
        expect(s.firstFailed?.id).toBe('b');
        expect(s.allDone).toBe(false);
    });

    it('treats an empty list as done', () => {
        expect(uploadSummary([]).allDone).toBe(true);
    });
});
