import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {Injector, runInInjectionContext} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {Router} from '@angular/router';
import {of, throwError} from 'rxjs';
import {SessionsPageComponent} from './sessions-page.component';
import {PersistentChatService} from '../../../core/services/persistent-chat.service';
import {ToastService} from '../../../core/services/toast.service';
import {UserService} from '../../../core/services/user.service';

/**
 * Create a SessionsPageComponent in a minimal injection context.
 */
function createComponent() {
    const mockHttp: any = {
        get: vi.fn().mockReturnValue(of({threads: []})),
        post: vi.fn().mockReturnValue(of({thread_id: 'new-thread-123'})),
        delete: vi.fn().mockReturnValue(of({})),
    };

    const mockRouter: any = {
        navigate: vi.fn(),
    };

    const mockChat: any = {
        isConnected: () => false,
        threadId: () => null,
    };

    const mockToast: any = {
        show: vi.fn(),
        success: vi.fn(),
        error: vi.fn(),
        dismiss: vi.fn(),
    };

    const mockUserService: any = {
        currentUserId: () => null,
        currentUser: () => null,
        users: () => [],
        isAuthenticated: () => false,
        isApproved: () => false,
        sessionReady: () => true,
    };

    const injector = Injector.create({
        providers: [
            {provide: HttpClient, useValue: mockHttp},
            {provide: Router, useValue: mockRouter},
            {provide: PersistentChatService, useValue: mockChat},
            {provide: ToastService, useValue: mockToast},
            {provide: UserService, useValue: mockUserService},
        ],
    });

    const component = runInInjectionContext(injector, () => new SessionsPageComponent());
    return {component, mockHttp, mockRouter, mockChat};
}

function makeThread(overrides: Partial<any> = {}) {
    return {
        id: 'thread-1',
        title: 'Test Session',
        status: 'active',
        config_name: 'interactive',
        permission_mode: 'supervised',
        created_at: '2026-01-01T00:00:00Z',
        last_activity: '2026-01-01T01:00:00Z',
        ended_at: null,
        total_turns: 5,
        project_id: null,
        ...overrides,
    };
}


describe('SessionsPageComponent', () => {
    let component: SessionsPageComponent;
    let mockHttp: any;
    let mockRouter: any;
    let mockChat: any;

    beforeEach(() => {
        const created = createComponent();
        component = created.component;
        mockHttp = created.mockHttp;
        mockRouter = created.mockRouter;
        mockChat = created.mockChat;
    });

    afterEach(() => {
        vi.clearAllMocks();
    });

    // =========================================================================
    // 9.2.1: Initial state
    // =========================================================================

    describe('initial state', () => {
        it('should start with empty threads', () => {
            expect(component.threads()).toEqual([]);
        });

        it('should start with loading true', () => {
            expect(component.loading()).toBe(true);
        });

        it('should start with creating false', () => {
            expect(component.creating()).toBe(false);
        });

        it('should start with null status filter', () => {
            expect(component.statusFilter()).toBeNull();
        });

        it('should have default form values', () => {
            expect(component.newConfig).toBe('interactive');
            expect(component.newPermission).toBe('supervised');
            expect(component.newModel).toBe('');
            expect(component.newTitle).toBe('');
        });
    });

    // =========================================================================
    // 9.2.2: loadThreads()
    // =========================================================================

    describe('loadThreads()', () => {
        it('should fetch threads from API', async () => {
            const threads = [makeThread(), makeThread({id: 'thread-2'})];
            mockHttp.get.mockReturnValue(of({threads}));

            await component.loadThreads();

            expect(mockHttp.get).toHaveBeenCalled();
            expect(component.threads()).toEqual(threads);
            expect(component.loading()).toBe(false);
        });

        it('should set loading false on error', async () => {
            mockHttp.get.mockReturnValue(throwError(() => new Error('fail')));

            await component.loadThreads();

            expect(component.loading()).toBe(false);
        });

        it('should handle missing threads in response', async () => {
            mockHttp.get.mockReturnValue(of({}));

            await component.loadThreads();

            expect(component.threads()).toEqual([]);
        });
    });

    // =========================================================================
    // 9.2.3: filteredThreads()
    // =========================================================================

    describe('filteredThreads()', () => {
        it('should return all threads when filter is null', () => {
            const threads = [
                makeThread({status: 'active'}),
                makeThread({id: 't2', status: 'ended'}),
            ];
            component.threads.set(threads);
            component.statusFilter.set(null);

            expect(component.filteredThreads().length).toBe(2);
        });

        it('should filter by active status', () => {
            component.threads.set([
                makeThread({status: 'active'}),
                makeThread({id: 't2', status: 'ended'}),
            ]);
            component.statusFilter.set('active');

            const filtered = component.filteredThreads();
            expect(filtered.length).toBe(1);
            expect(filtered[0].status).toBe('active');
        });

        it('should filter by ended status', () => {
            component.threads.set([
                makeThread({status: 'active'}),
                makeThread({id: 't2', status: 'ended'}),
            ]);
            component.statusFilter.set('ended');

            const filtered = component.filteredThreads();
            expect(filtered.length).toBe(1);
            expect(filtered[0].status).toBe('ended');
        });
    });

    // =========================================================================
    // 9.2.4: createSession()
    // =========================================================================

    describe('createSession()', () => {
        it('should POST to create thread endpoint', async () => {
            mockHttp.post.mockReturnValue(of({thread_id: 'new-123'}));

            await component.createSession();

            expect(mockHttp.post).toHaveBeenCalled();
            const [url, body] = mockHttp.post.mock.calls[0];
            expect(url).toContain('/persistent/threads');
        });

        it('should include title, config, and permission in body', async () => {
            component.newTitle = 'My Session';
            component.newConfig = 'developer';
            component.newPermission = 'autonomous';

            mockHttp.post.mockReturnValue(of({thread_id: 'new-123'}));

            await component.createSession();

            const body = mockHttp.post.mock.calls[0][1];
            expect(body.title).toBe('My Session');
            expect(body.config_name).toBe('developer');
            expect(body.permission_mode).toBe('autonomous');
        });

        it('should use "Untitled Session" when title is empty', async () => {
            component.newTitle = '';
            mockHttp.post.mockReturnValue(of({thread_id: 'new-123'}));

            await component.createSession();

            const body = mockHttp.post.mock.calls[0][1];
            expect(body.title).toBe('Untitled Session');
        });

        it('should include model when specified', async () => {
            component.newModel = 'gpt-5.4';
            mockHttp.post.mockReturnValue(of({thread_id: 'new-123'}));

            await component.createSession();

            const body = mockHttp.post.mock.calls[0][1];
            expect(body.model).toBe('gpt-5.4');
        });

        it('should NOT include model when empty', async () => {
            component.newModel = '';
            mockHttp.post.mockReturnValue(of({thread_id: 'new-123'}));

            await component.createSession();

            const body = mockHttp.post.mock.calls[0][1];
            expect(body.model).toBeUndefined();
        });

        it('should include project_ids when selected', async () => {
            component.selectedProjectIds.set(['proj-1', 'proj-2']);
            mockHttp.post.mockReturnValue(of({thread_id: 'new-123'}));

            await component.createSession();

            const body = mockHttp.post.mock.calls[0][1];
            expect(body.project_ids).toEqual(['proj-1', 'proj-2']);
        });

        it('should navigate to new session on success', async () => {
            mockHttp.post.mockReturnValue(of({thread_id: 'new-123'}));

            await component.createSession();

            expect(mockRouter.navigate).toHaveBeenCalledWith(['/sessions', 'new-123']);
        });

        it('should reset form state after creation', async () => {
            component.newTitle = 'Title';
            component.newModel = 'gpt-5.4';
            component.selectedProjectIds.set(['proj-1']);
            component.showCreate = true;

            mockHttp.post.mockReturnValue(of({thread_id: 'new-123'}));

            await component.createSession();

            expect(component.newTitle).toBe('');
            expect(component.newModel).toBe('');
            expect(component.selectedProjectIds()).toEqual([]);
            expect(component.showCreate).toBe(false);
        });

        it('should set creating signal during creation', async () => {
            let resolvePost: (value: any) => void;
            const postPromise = new Promise(r => {
                resolvePost = r;
            });
            mockHttp.post.mockReturnValue(of({thread_id: 'new-123'}));

            await component.createSession();

            // After completion, creating should be false
            expect(component.creating()).toBe(false);
        });

        it('should handle creation failure gracefully', async () => {
            mockHttp.post.mockReturnValue(throwError(() => new Error('fail')));

            await component.createSession();

            expect(component.creating()).toBe(false);
            expect(mockRouter.navigate).not.toHaveBeenCalled();
        });
    });

    // =========================================================================
    // 9.2.5: resumeSession()
    // =========================================================================

    describe('resumeSession()', () => {
        it('should navigate to session page', () => {
            const thread = makeThread({id: 'thread-abc'});
            component.resumeSession(thread);

            expect(mockRouter.navigate).toHaveBeenCalledWith(['/sessions', 'thread-abc']);
        });
    });

    // =========================================================================
    // 9.2.6: endSession()
    // =========================================================================

    describe('endSession()', () => {
        it('should call DELETE endpoint', async () => {
            vi.spyOn(globalThis, 'confirm').mockReturnValue(true);
            const thread = makeThread({id: 'thread-xyz'});
            await component.endSession(thread);

            expect(mockHttp.delete).toHaveBeenCalled();
            const url = mockHttp.delete.mock.calls[0][0];
            expect(url).toContain('thread-xyz');
        });

        it('should reload threads after ending', async () => {
            vi.spyOn(globalThis, 'confirm').mockReturnValue(true);
            const loadSpy = vi.spyOn(component, 'loadThreads');
            await component.endSession(makeThread());

            expect(loadSpy).toHaveBeenCalled();
        });
    });

    // =========================================================================
    // 9.2.7: toggleProject() / isProjectSelected()
    // =========================================================================

    describe('project selection', () => {
        it('should add project to selection', () => {
            component.toggleProject('proj-1');
            expect(component.selectedProjectIds()).toContain('proj-1');
            expect(component.isProjectSelected('proj-1')).toBe(true);
        });

        it('should remove project from selection on second toggle', () => {
            component.toggleProject('proj-1');
            component.toggleProject('proj-1');
            expect(component.selectedProjectIds()).not.toContain('proj-1');
            expect(component.isProjectSelected('proj-1')).toBe(false);
        });

        it('should handle multiple project selections', () => {
            component.toggleProject('proj-1');
            component.toggleProject('proj-2');
            expect(component.selectedProjectIds()).toEqual(['proj-1', 'proj-2']);
        });
    });

    // =========================================================================
    // 9.2.8: returnToActive()
    // =========================================================================

    describe('returnToActive()', () => {
        it('should navigate to thread when threadId exists', () => {
            mockChat.threadId = () => 'thread-abc';
            component.returnToActive();

            expect(mockRouter.navigate).toHaveBeenCalledWith(['/sessions', 'thread-abc']);
        });

        it('should not navigate when threadId is null', () => {
            mockChat.threadId = () => null;
            component.returnToActive();

            expect(mockRouter.navigate).not.toHaveBeenCalled();
        });
    });
});
