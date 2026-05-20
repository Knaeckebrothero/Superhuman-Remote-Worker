import {describe, expect, it, vi} from 'vitest';
import {Injector, runInInjectionContext} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {BuilderStreamService} from './builder-stream.service';
import {JobArtifactService} from './job-artifact.service';
import {UserService} from './user.service';

// ---------------------------------------------------------------------------
// Test scaffolding
// ---------------------------------------------------------------------------

function emptyReadableBody() {
    return {
        getReader: () => ({
            read: () => Promise.resolve({done: true, value: undefined}),
        }),
    };
}

function createService(): {service: BuilderStreamService; fetchMock: ReturnType<typeof vi.fn>} {
    const fetchMock = vi.fn().mockResolvedValue({
        ok: true,
        body: emptyReadableBody(),
    });
    (globalThis as any).fetch = fetchMock;

    const mockArtifacts = {
        streaming: {set: vi.fn()},
        builderModel: () => 'test-model',
        instructions: () => null,
        config: () => null,
        description: () => null,
        activeJobId: () => null,
        applyToolCall: vi.fn(),
    } as unknown as JobArtifactService;

    const mockUser = {
        currentUserId: () => 'user-1',
    } as unknown as UserService;

    const injector = Injector.create({
        providers: [
            {provide: HttpClient, useValue: {} as HttpClient},
            {provide: JobArtifactService, useValue: mockArtifacts},
            {provide: UserService, useValue: mockUser},
        ],
    });

    const service = runInInjectionContext(injector, () => new BuilderStreamService());
    return {service, fetchMock};
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('BuilderStreamService — sendMessage cookie BFF compatibility', () => {
    it('sends the srw_session cookie cross-origin by setting credentials: "include"', async () => {
        // The cockpit and orchestrator live on different origins
        // (e.g. cockpit.example vs api.example). The HttpOnly srw_session
        // cookie only rides along on cross-origin fetches when the call
        // explicitly opts in via credentials: 'include' — the default
        // ('same-origin') drops the cookie and the orchestrator's
        // _get_user_from_mcp_headers fall-through returns 401.
        const {service, fetchMock} = createService();

        await service.sendMessage('sess-abc', 'hello', {});

        expect(fetchMock).toHaveBeenCalledTimes(1);
        const init = fetchMock.mock.calls[0][1] as RequestInit;
        expect(init.credentials).toBe('include');
    });

    it('sets the X-CSRF: 1 header that the orchestrator CSRF middleware requires for cookie-auth POSTs', async () => {
        // orchestrator/security/csrf.py rejects state-changing cookie-
        // authenticated requests without X-CSRF. The Angular authInterceptor
        // adds this for HttpClient calls automatically; raw fetch() must
        // set it explicitly.
        const {service, fetchMock} = createService();

        await service.sendMessage('sess-abc', 'hello', {});

        const init = fetchMock.mock.calls[0][1] as RequestInit;
        const headers = init.headers as Record<string, string>;
        expect(headers['X-CSRF']).toBe('1');
    });
});
