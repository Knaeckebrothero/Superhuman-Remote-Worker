import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {signal} from '@angular/core';
import {TestBed} from '@angular/core/testing';

import {CitationsPanelComponent} from './citations-panel.component';
import {
    PersistentChatService,
    ThreadCitation,
} from '../../../core/services/persistent-chat.service';

function citation(over: Partial<ThreadCitation> = {}): ThreadCitation {
    return {
        id: 1,
        claim: 'A claim',
        source_id: 10,
        source_name: 'Example Source',
        source_type: 'website',
        source_identifier: 'https://example.com',
        verification_status: 'verified',
        confidence: 'high',
        created_at: '2026-06-24T00:00:00Z',
        ...over,
    };
}

describe('CitationsPanelComponent', () => {
    let citationsByCid: ReturnType<typeof signal<Map<number, ThreadCitation>>>;
    let mockChat: {
        citationsByCid: typeof citationsByCid;
        fetchCitationDrift: ReturnType<typeof vi.fn>;
        fetchCitationSnapshotBlob: ReturnType<typeof vi.fn>;
    };

    beforeEach(() => {
        citationsByCid = signal(new Map<number, ThreadCitation>());
        mockChat = {
            citationsByCid,
            fetchCitationDrift: vi.fn(),
            fetchCitationSnapshotBlob: vi.fn(),
        };
        TestBed.resetTestingModule();
        TestBed.configureTestingModule({
            providers: [{provide: PersistentChatService, useValue: mockChat}],
        });
    });

    afterEach(() => {
        // Restore window.open etc. — vi.spyOn returns the same spy on repeat, so
        // call history would otherwise leak between tests.
        vi.restoreAllMocks();
    });

    function make(): CitationsPanelComponent {
        return TestBed.runInInjectionContext(
            () => new CitationsPanelComponent(),
        ) as CitationsPanelComponent;
    }

    it('rows sorts by id ascending and numbers them 1..k', () => {
        citationsByCid.set(
            new Map([
                [24, citation({id: 24})],
                [12, citation({id: 12})],
            ]),
        );
        const rows = make().rows();
        expect(rows.map((r) => r.id)).toEqual([12, 24]);
        expect(rows.map((r) => r.index)).toEqual([1, 2]);
    });

    it('flags only http(s) sources as web links', () => {
        citationsByCid.set(
            new Map([
                [1, citation({id: 1, source_identifier: 'https://example.com'})],
                [2, citation({id: 2, source_identifier: '/Personal/doc.pdf'})],
                [3, citation({id: 3, source_identifier: null})],
            ]),
        );
        const rows = make().rows();
        expect(rows.find((r) => r.id === 1)!.isWebLink).toBe(true);
        expect(rows.find((r) => r.id === 2)!.isWebLink).toBe(false);
        expect(rows.find((r) => r.id === 3)!.isWebLink).toBe(false);
    });

    it('maps verification status to badge tones + labels', () => {
        const c = make();
        expect(c.statusTone('verified')).toBe('success');
        expect(c.statusTone('failed')).toBe('danger');
        expect(c.statusTone('pending')).toBe('warning');
        expect(c.statusTone('weird')).toBe('neutral');
        expect(c.statusLabel('verified')).toBe('chat.citations.statusVerified');
        expect(c.statusLabel('failed')).toBe('chat.citations.statusFailed');
    });

    it('maps drift state to badge tones + labels', () => {
        const c = make();
        expect(c.driftTone('unchanged')).toBe('success');
        expect(c.driftTone('changed')).toBe('alert');
        expect(c.driftTone('unreachable')).toBe('neutral');
        expect(c.driftLabel('unreachable')).toBe('chat.citations.driftUnreachable');
    });

    it('checkDrift fetches once, caches, and is a no-op on repeat', async () => {
        mockChat.fetchCitationDrift.mockResolvedValue({
            citation_id: 5,
            live_state: 'changed',
            snapshot_available: true,
        });
        const c = make();
        await c.checkDrift(5);
        expect(mockChat.fetchCitationDrift).toHaveBeenCalledWith(5);
        expect(c.driftOf(5)).toMatchObject({live_state: 'changed'});

        await c.checkDrift(5);
        expect(mockChat.fetchCitationDrift).toHaveBeenCalledTimes(1);
    });

    it('checkDrift falls back to unknown when the fetch returns null', async () => {
        mockChat.fetchCitationDrift.mockResolvedValue(null);
        const c = make();
        await c.checkDrift(5);
        expect(c.driftOf(5)).toMatchObject({live_state: 'unknown'});
    });

    it('viewOriginal opens an object URL for the fetched snapshot blob', async () => {
        const blob = new Blob(['pdf'], {type: 'application/pdf'});
        mockChat.fetchCitationSnapshotBlob.mockResolvedValue(blob);
        const openSpy = vi.spyOn(window, 'open').mockReturnValue(null);
        (URL as unknown as {createObjectURL: unknown}).createObjectURL = vi
            .fn()
            .mockReturnValue('blob:abc');
        (URL as unknown as {revokeObjectURL: unknown}).revokeObjectURL = vi.fn();

        await make().viewOriginal(9);

        expect(mockChat.fetchCitationSnapshotBlob).toHaveBeenCalledWith(9);
        expect(URL.createObjectURL).toHaveBeenCalledWith(blob);
        expect(openSpy).toHaveBeenCalledWith('blob:abc', '_blank');
    });

    it('viewOriginal is a no-op when no snapshot is available', async () => {
        mockChat.fetchCitationSnapshotBlob.mockResolvedValue(null);
        const openSpy = vi.spyOn(window, 'open').mockReturnValue(null);
        await make().viewOriginal(9);
        expect(openSpy).not.toHaveBeenCalled();
    });
});
