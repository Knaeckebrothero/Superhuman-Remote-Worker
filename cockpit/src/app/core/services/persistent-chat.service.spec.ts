import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {Injector, runInInjectionContext} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {of, throwError} from 'rxjs';
import {PersistentChatService} from './persistent-chat.service';

/**
 * Create a mock WebSocket that captures handlers and allows triggering events.
 */
function createMockWs() {
    const ws: any = {
        readyState: 1, // OPEN
        send: vi.fn(),
        close: vi.fn(),
        onopen: null as ((ev: any) => void) | null,
        onmessage: null as ((ev: any) => void) | null,
        onclose: null as ((ev: any) => void) | null,
        onerror: null as (() => void) | null,
    };
    return ws;
}

/**
 * Create a PersistentChatService in a minimal injection context.
 */
function createService() {
    const mockHttp: any = {
        get: vi.fn().mockReturnValue(of({messages: [], total: 0})),
        post: vi.fn().mockReturnValue(of({})),
        delete: vi.fn().mockReturnValue(of({})),
    };

    const injector = Injector.create({
        providers: [{provide: HttpClient, useValue: mockHttp}],
    });

    const service = runInInjectionContext(injector, () => new PersistentChatService());
    return {service, mockHttp};
}

/**
 * Helper: connect a service and return the mock WS.
 * Patches globalThis.WebSocket to intercept construction.
 */
async function connectService(service: PersistentChatService) {
    const ws = createMockWs();
    // Must use a regular function (not arrow) — arrow functions can't be called with `new`.
    const WsSpy = vi.fn();
    (globalThis as any).WebSocket = function MockWebSocket() {
        WsSpy(...arguments);
        return ws;
    } as any;
    // Add OPEN constant so `send()` guard works
    (globalThis as any).WebSocket.OPEN = 1;

    await service.connect('test-thread-123');

    // Trigger onopen
    if (ws.onopen) ws.onopen({});

    // Simulate agent ready so sendMessage() sends immediately (not queued)
    fireWsMessage(ws, {method: 'ready'});
    // Clear the send spy so tests only see their own calls
    ws.send.mockClear();

    return {ws, WsSpy};
}

function fireWsMessage(ws: any, data: Record<string, unknown>) {
    if (ws.onmessage) {
        ws.onmessage({data: JSON.stringify(data)});
    }
}


describe('PersistentChatService', () => {
    let service: PersistentChatService;
    let mockHttp: any;
    let originalWebSocket: any;

    beforeEach(() => {
        originalWebSocket = (globalThis as any).WebSocket;
        const created = createService();
        service = created.service;
        mockHttp = created.mockHttp;
    });

    afterEach(() => {
        (globalThis as any).WebSocket = originalWebSocket;
        vi.clearAllMocks();
    });

    // =========================================================================
    // 9.1.1: Initial signal state
    // =========================================================================

    describe('initial state', () => {
        it('should start disconnected', () => {
            expect(service.connectionState()).toBe('disconnected');
        });

        it('should start with isConnected false', () => {
            expect(service.isConnected()).toBe(false);
        });

        it('should start with null threadId', () => {
            expect(service.threadId()).toBeNull();
        });

        it('should start with empty messages', () => {
            expect(service.messages()).toEqual([]);
        });

        it('should start with empty streamingText', () => {
            expect(service.streamingText()).toBe('');
        });

        it('should start with isStreaming false', () => {
            expect(service.isStreaming()).toBe(false);
        });

        it('should start with supervised permission mode', () => {
            expect(service.permissionMode()).toBe('supervised');
        });

        it('should start with null pendingPermission', () => {
            expect(service.pendingPermission()).toBeNull();
        });

        it('should start with null error', () => {
            expect(service.error()).toBeNull();
        });

        it('should start with isSessionPaused false', () => {
            expect(service.isSessionPaused()).toBe(false);
        });
    });

    // =========================================================================
    // 9.1.2: connect()
    // =========================================================================

    describe('connect()', () => {
        it('should set connectionState to connecting', async () => {
            const ws = createMockWs();
            (globalThis as any).WebSocket = function MockWebSocket() {
                return ws;
            } as any;
            (globalThis as any).WebSocket.OPEN = 1;

            const promise = service.connect('tid-1');
            await promise;

            // Before onopen fires, state should be connecting
            expect(service.connectionState()).toBe('connecting');
        });

        it('should set connectionState to connected on WS open', async () => {
            const {ws} = await connectService(service);
            expect(service.connectionState()).toBe('connected');
            expect(service.isConnected()).toBe(true);
        });

        it('should set threadId for proxied connection', async () => {
            await connectService(service);
            expect(service.threadId()).toBe('test-thread-123');
        });

        it('should load history for proxied connection', async () => {
            mockHttp.get.mockReturnValue(of({
                messages: [
                    {
                        id: '1',
                        role: 'human',
                        content: 'Hi',
                        tool_calls: null,
                        turn_number: 1,
                        created_at: '2026-01-01T00:00:00Z'
                    },
                    {
                        id: '2',
                        role: 'ai',
                        content: 'Hello!',
                        tool_calls: null,
                        turn_number: 1,
                        created_at: '2026-01-01T00:00:01Z'
                    },
                ],
                total: 2,
            }));

            await connectService(service);

            const msgs = service.messages();
            expect(msgs.length).toBe(2);
            expect(msgs[0].role).toBe('user');
            expect(msgs[1].role).toBe('assistant');
            expect(msgs[0].historical).toBe(true);
        });

        it('should derive WS URL from API URL', async () => {
            const {WsSpy} = await connectService(service);
            const wsUrl = WsSpy.mock.calls[0][0];
            expect(wsUrl).toContain('/ws/persistent/test-thread-123');
            expect(wsUrl).toMatch(/^ws:\/\//);
        });

        it('should clear previous messages on reconnect', async () => {
            // First connect
            mockHttp.get.mockReturnValue(of({
                messages: [
                    {
                        id: '1',
                        role: 'human',
                        content: 'Old',
                        tool_calls: null,
                        turn_number: 1,
                        created_at: '2026-01-01T00:00:00Z'
                    },
                ],
                total: 1,
            }));
            await connectService(service);
            expect(service.messages().length).toBe(1);

            // Reconnect (messages should be cleared then history reloaded)
            mockHttp.get.mockReturnValue(of({messages: [], total: 0}));
            await connectService(service);
            expect(service.messages()).toEqual([]);
        });

        it('should handle history load failure gracefully', async () => {
            mockHttp.get.mockReturnValue(throwError(() => new Error('Network error')));
            await connectService(service);

            // Should still be connected (history failure is non-fatal)
            expect(service.historyLoaded()).toBe(true);
            expect(service.messages()).toEqual([]);
        });
    });

    // =========================================================================
    // 9.1.3: loadHistory() — role mapping
    // =========================================================================

    describe('loadHistory() role mapping', () => {
        it('should map "human" role to "user"', async () => {
            mockHttp.get.mockReturnValue(of({
                messages: [
                    {
                        id: '1',
                        role: 'human',
                        content: 'Test',
                        tool_calls: null,
                        turn_number: 1,
                        created_at: '2026-01-01T00:00:00Z'
                    },
                ],
                total: 1,
            }));

            await connectService(service);
            expect(service.messages()[0].role).toBe('user');
        });

        it('should map "ai" role to "assistant"', async () => {
            mockHttp.get.mockReturnValue(of({
                messages: [
                    {
                        id: '1',
                        role: 'ai',
                        content: 'Response',
                        tool_calls: null,
                        turn_number: 1,
                        created_at: '2026-01-01T00:00:00Z'
                    },
                ],
                total: 1,
            }));

            await connectService(service);
            expect(service.messages()[0].role).toBe('assistant');
        });

        it('should map "user" role to "user"', async () => {
            mockHttp.get.mockReturnValue(of({
                messages: [
                    {
                        id: '1',
                        role: 'user',
                        content: 'Test',
                        tool_calls: null,
                        turn_number: 1,
                        created_at: '2026-01-01T00:00:00Z'
                    },
                ],
                total: 1,
            }));

            await connectService(service);
            expect(service.messages()[0].role).toBe('user');
        });

        it('should map "assistant" role to "assistant"', async () => {
            mockHttp.get.mockReturnValue(of({
                messages: [
                    {
                        id: '1',
                        role: 'assistant',
                        content: 'Hi',
                        tool_calls: null,
                        turn_number: 1,
                        created_at: '2026-01-01T00:00:00Z'
                    },
                ],
                total: 1,
            }));

            await connectService(service);
            expect(service.messages()[0].role).toBe('assistant');
        });

        it('should filter out non-user/assistant roles (e.g. tool)', async () => {
            mockHttp.get.mockReturnValue(of({
                messages: [
                    {
                        id: '1',
                        role: 'human',
                        content: 'Hi',
                        tool_calls: null,
                        turn_number: 1,
                        created_at: '2026-01-01T00:00:00Z'
                    },
                    {
                        id: '2',
                        role: 'tool',
                        content: 'result',
                        tool_calls: null,
                        turn_number: 1,
                        created_at: '2026-01-01T00:00:01Z'
                    },
                    {
                        id: '3',
                        role: 'ai',
                        content: 'Done',
                        tool_calls: null,
                        turn_number: 1,
                        created_at: '2026-01-01T00:00:02Z'
                    },
                ],
                total: 3,
            }));

            await connectService(service);
            expect(service.messages().length).toBe(2);
        });

        it('should parse tool_calls from history', async () => {
            mockHttp.get.mockReturnValue(of({
                messages: [
                    {
                        id: '1', role: 'ai', content: 'Using tool...',
                        tool_calls: [{id: 'tc1', name: 'web_search', args: {query: 'test'}}],
                        turn_number: 1, created_at: '2026-01-01T00:00:00Z',
                    },
                ],
                total: 1,
            }));

            await connectService(service);
            const msg = service.messages()[0];
            expect(msg.toolCalls).toBeDefined();
            expect(msg.toolCalls![0].tool).toBe('web_search');
            expect(msg.toolCalls![0].status).toBe('completed');
        });
    });

    // =========================================================================
    // 9.1.4: sendMessage()
    // =========================================================================

    describe('sendMessage()', () => {
        it('should send message method over WS', async () => {
            const {ws} = await connectService(service);
            service.sendMessage('Hello agent');

            expect(ws.send).toHaveBeenCalledWith(
                JSON.stringify({method: 'message', content: 'Hello agent'})
            );
        });

        it('should add user message to local messages', async () => {
            await connectService(service);
            service.sendMessage('Test message');

            const msgs = service.messages();
            const last = msgs[msgs.length - 1];
            expect(last.role).toBe('user');
            expect(last.content).toBe('Test message');
        });

        it('should not send empty messages', async () => {
            const {ws} = await connectService(service);
            service.sendMessage('');
            service.sendMessage('   ');

            expect(ws.send).not.toHaveBeenCalled();
        });

        it('should queue message when WS is null', () => {
            // Not connected — ws is null; message is queued (shown in UI) but not sent
            service.sendMessage('test');
            // Should not throw, message is added locally for display
            expect(service.messages().length).toBe(1);
            expect(service.messages()[0].content).toBe('test');
            // Queued for later delivery
            expect(service.pendingMessage()).toBe('test');
        });

        it('should set isWaitingForInput to false', async () => {
            await connectService(service);
            // Simulate ready state
            service.isWaitingForInput.set(true);

            service.sendMessage('Go');
            expect(service.isWaitingForInput()).toBe(false);
        });
    });

    // =========================================================================
    // 9.1.5: Slash commands
    // =========================================================================

    describe('slash commands', () => {
        it('/compact sends compact method', async () => {
            const {ws} = await connectService(service);
            service.sendMessage('/compact');

            expect(ws.send).toHaveBeenCalledWith(
                JSON.stringify({method: 'compact', focus: ''})
            );
        });

        it('/compact with focus sends focus arg', async () => {
            const {ws} = await connectService(service);
            service.sendMessage('/compact database schema');

            expect(ws.send).toHaveBeenCalledWith(
                JSON.stringify({method: 'compact', focus: 'database schema'})
            );
        });

        it('/compact adds system message', async () => {
            await connectService(service);
            service.sendMessage('/compact');

            const msgs = service.messages();
            const last = msgs[msgs.length - 1];
            expect(last.role).toBe('system');
            expect(last.content).toContain('Compacting');
        });

        it('/done sends archive method', async () => {
            const {ws} = await connectService(service);
            service.sendMessage('/done');

            expect(ws.send).toHaveBeenCalledWith(
                JSON.stringify({method: 'archive'})
            );
        });

        it('/done adds system message', async () => {
            await connectService(service);
            service.sendMessage('/done');

            const msgs = service.messages();
            const last = msgs[msgs.length - 1];
            expect(last.role).toBe('system');
            expect(last.content).toContain('Ending session');
        });

        it('/auto sets mode to auto_accept', async () => {
            const {ws} = await connectService(service);
            service.sendMessage('/auto');

            expect(ws.send).toHaveBeenCalledWith(
                JSON.stringify({method: 'mode.set', mode: 'auto_accept'})
            );
            expect(service.permissionMode()).toBe('auto_accept');
        });

        it('/supervised sets mode to supervised', async () => {
            const {ws} = await connectService(service);
            service.permissionMode.set('auto_accept');
            service.sendMessage('/supervised');

            expect(ws.send).toHaveBeenCalledWith(
                JSON.stringify({method: 'mode.set', mode: 'supervised'})
            );
            expect(service.permissionMode()).toBe('supervised');
        });

        it('/autonomous sets mode to autonomous', async () => {
            const {ws} = await connectService(service);
            service.sendMessage('/autonomous');

            expect(ws.send).toHaveBeenCalledWith(
                JSON.stringify({method: 'mode.set', mode: 'autonomous'})
            );
            expect(service.permissionMode()).toBe('autonomous');
        });

        it('unknown slash command is sent as normal message', async () => {
            const {ws} = await connectService(service);
            service.sendMessage('/unknown stuff');

            expect(ws.send).toHaveBeenCalledWith(
                JSON.stringify({method: 'message', content: '/unknown stuff'})
            );
        });
    });

    // =========================================================================
    // 9.1.6: approve() / deny() / interrupt()
    // =========================================================================

    describe('approve/deny/interrupt', () => {
        it('approve sends approve method and clears pending', async () => {
            const {ws} = await connectService(service);
            service.pendingPermission.set({tool: 'run_command', args: {}});

            service.approve();

            expect(ws.send).toHaveBeenCalledWith(JSON.stringify({method: 'approve'}));
            expect(service.pendingPermission()).toBeNull();
        });

        it('deny sends deny method and clears pending', async () => {
            const {ws} = await connectService(service);
            service.pendingPermission.set({tool: 'run_command', args: {}});

            service.deny();

            expect(ws.send).toHaveBeenCalledWith(JSON.stringify({method: 'deny'}));
            expect(service.pendingPermission()).toBeNull();
        });

        it('interrupt sends interrupt method', async () => {
            const {ws} = await connectService(service);
            service.interrupt();

            expect(ws.send).toHaveBeenCalledWith(JSON.stringify({method: 'interrupt'}));
        });
    });

    // =========================================================================
    // 9.1.7: setMode()
    // =========================================================================

    describe('setMode()', () => {
        it('sends mode.set and updates local signal', async () => {
            const {ws} = await connectService(service);
            service.setMode('autonomous');

            expect(ws.send).toHaveBeenCalledWith(
                JSON.stringify({method: 'mode.set', mode: 'autonomous'})
            );
            expect(service.permissionMode()).toBe('autonomous');
        });
    });

    // =========================================================================
    // 9.1.8: disconnect()
    // =========================================================================

    describe('disconnect()', () => {
        it('should close WS with code 1000', async () => {
            const {ws} = await connectService(service);
            service.disconnect();

            expect(ws.close).toHaveBeenCalledWith(1000);
        });

        it('should reset state signals', async () => {
            await connectService(service);
            service.disconnect();

            expect(service.connectionState()).toBe('disconnected');
            expect(service.isStreaming()).toBe(false);
            expect(service.isWaitingForInput()).toBe(false);
            expect(service.pendingPermission()).toBeNull();
        });

        it('should nullify WS onclose to prevent error handler', async () => {
            const {ws} = await connectService(service);
            service.disconnect();

            expect(ws.onclose).toBeNull();
        });

        it('should clear reconnect timer', async () => {
            await connectService(service);
            // Simulate a reconnect timer
            (service as any).reconnectTimer = setTimeout(() => {
            }, 10000);

            service.disconnect();

            expect((service as any).reconnectTimer).toBeNull();
        });
    });

    // =========================================================================
    // 9.1.9: clearMessages()
    // =========================================================================

    describe('clearMessages()', () => {
        it('should clear all messages', async () => {
            await connectService(service);
            service.sendMessage('Hello');
            expect(service.messages().length).toBeGreaterThan(0);

            service.clearMessages();
            expect(service.messages()).toEqual([]);
        });
    });

    // =========================================================================
    // 9.1.10: WS message handlers
    // =========================================================================

    describe('WS message handlers', () => {
        describe('token', () => {
            it('should append to streamingText', async () => {
                const {ws} = await connectService(service);
                fireWsMessage(ws, {method: 'token', params: {content: 'Hello'}});
                fireWsMessage(ws, {method: 'token', params: {content: ' world'}});

                expect(service.streamingText()).toBe('Hello world');
            });
        });

        describe('turn.started', () => {
            it('should set isStreaming and reset streamingText', async () => {
                const {ws} = await connectService(service);
                service.streamingText.set('leftover');

                fireWsMessage(ws, {method: 'turn.started', params: {turn_id: 5}});

                expect(service.isStreaming()).toBe(true);
                expect(service.streamingText()).toBe('');
                expect(service.currentTurnId()).toBe(5);
                expect(service.currentToolCalls()).toEqual([]);
            });
        });

        describe('turn.completed', () => {
            it('should finalize streaming and reset turn', async () => {
                const {ws} = await connectService(service);

                // Simulate a turn
                fireWsMessage(ws, {method: 'turn.started', params: {turn_id: 3}});
                fireWsMessage(ws, {method: 'token', params: {content: 'Done'}});
                fireWsMessage(ws, {method: 'turn.completed', params: {}});

                expect(service.isStreaming()).toBe(false);
                expect(service.currentTurnId()).toBeNull();
                expect(service.streamingText()).toBe('');

                // Message should be finalized
                const msgs = service.messages();
                const last = msgs[msgs.length - 1];
                expect(last.role).toBe('assistant');
                expect(last.content).toBe('Done');
            });
        });

        describe('tool.started', () => {
            it('should add to currentToolCalls', async () => {
                const {ws} = await connectService(service);
                fireWsMessage(ws, {
                    method: 'tool.started',
                    params: {id: 'tc1', tool: 'web_search', args: {query: 'test'}},
                });

                const calls = service.currentToolCalls();
                expect(calls.length).toBe(1);
                expect(calls[0].id).toBe('tc1');
                expect(calls[0].tool).toBe('web_search');
                expect(calls[0].status).toBe('running');
            });
        });

        describe('tool.completed', () => {
            it('should update matching tool call', async () => {
                const {ws} = await connectService(service);
                fireWsMessage(ws, {
                    method: 'tool.started',
                    params: {id: 'tc1', tool: 'web_search', args: {}},
                });
                fireWsMessage(ws, {
                    method: 'tool.completed',
                    params: {id: 'tc1', result: 'Found 3 results'},
                });

                const calls = service.currentToolCalls();
                expect(calls[0].status).toBe('completed');
                expect(calls[0].result).toBe('Found 3 results');
            });

            it('should not modify other tool calls', async () => {
                const {ws} = await connectService(service);
                fireWsMessage(ws, {
                    method: 'tool.started',
                    params: {id: 'tc1', tool: 'web_search', args: {}},
                });
                fireWsMessage(ws, {
                    method: 'tool.started',
                    params: {id: 'tc2', tool: 'read_file', args: {}},
                });
                fireWsMessage(ws, {
                    method: 'tool.completed',
                    params: {id: 'tc1', result: 'done'},
                });

                const calls = service.currentToolCalls();
                expect(calls[0].status).toBe('completed');
                expect(calls[1].status).toBe('running');
            });
        });

        describe('permission.request', () => {
            it('should set pendingPermission', async () => {
                const {ws} = await connectService(service);
                fireWsMessage(ws, {
                    method: 'permission.request',
                    params: {tool: 'run_command', args: {command: 'rm -rf /'}},
                });

                const perm = service.pendingPermission();
                expect(perm).not.toBeNull();
                expect(perm!.tool).toBe('run_command');
                expect(perm!.args).toEqual({command: 'rm -rf /'});
            });
        });

        describe('mode.changed', () => {
            it('should update permissionMode', async () => {
                const {ws} = await connectService(service);
                fireWsMessage(ws, {method: 'mode.changed', params: {mode: 'autonomous'}});

                expect(service.permissionMode()).toBe('autonomous');
            });

            it('should default to supervised if mode missing', async () => {
                const {ws} = await connectService(service);
                fireWsMessage(ws, {method: 'mode.changed', params: {}});

                expect(service.permissionMode()).toBe('supervised');
            });
        });

        describe('session.state', () => {
            it('should sync permissionMode', async () => {
                const {ws} = await connectService(service);
                fireWsMessage(ws, {
                    method: 'session.state',
                    params: {permission_mode: 'auto_accept'},
                });

                expect(service.permissionMode()).toBe('auto_accept');
            });
        });

        describe('greeting', () => {
            it('should add greeting message and set waiting', async () => {
                const {ws} = await connectService(service);
                fireWsMessage(ws, {
                    method: 'greeting',
                    params: {content: 'Hello! How can I help?'},
                });

                const msgs = service.messages();
                const last = msgs[msgs.length - 1];
                expect(last.role).toBe('assistant');
                expect(last.content).toBe('Hello! How can I help?');
                expect(service.isWaitingForInput()).toBe(true);
            });
        });

        describe('ready', () => {
            it('should set isWaitingForInput and stop streaming', async () => {
                const {ws} = await connectService(service);
                service.isStreaming.set(true);
                service.streamingText.set('partial');

                fireWsMessage(ws, {method: 'ready', params: {}});

                expect(service.isWaitingForInput()).toBe(true);
                expect(service.isStreaming()).toBe(false);
            });

            it('should finalize streaming text into message', async () => {
                const {ws} = await connectService(service);
                service.streamingText.set('Final answer');

                fireWsMessage(ws, {method: 'ready', params: {}});

                const msgs = service.messages();
                const last = msgs[msgs.length - 1];
                expect(last.role).toBe('assistant');
                expect(last.content).toBe('Final answer');
            });
        });

        describe('error', () => {
            it('should set error signal', async () => {
                const {ws} = await connectService(service);
                fireWsMessage(ws, {
                    method: 'error',
                    params: {message: 'Something went wrong'},
                });

                expect(service.error()).toBe('Something went wrong');
            });

            it('should default to "Unknown error"', async () => {
                const {ws} = await connectService(service);
                fireWsMessage(ws, {method: 'error', params: {}});

                expect(service.error()).toBe('Unknown error');
            });
        });

        describe('session.ended', () => {
            it('should add system message and stop waiting', async () => {
                const {ws} = await connectService(service);
                service.isWaitingForInput.set(true);

                fireWsMessage(ws, {method: 'session.ended', params: {}});

                const msgs = service.messages();
                const last = msgs[msgs.length - 1];
                expect(last.role).toBe('system');
                expect(last.content).toBe('Session ended.');
                expect(service.isWaitingForInput()).toBe(false);
            });
        });

        describe('context.compacted', () => {
            it('should add system message with counts', async () => {
                const {ws} = await connectService(service);
                fireWsMessage(ws, {
                    method: 'context.compacted',
                    params: {before: 150, after: 30},
                });

                const msgs = service.messages();
                const last = msgs[msgs.length - 1];
                expect(last.role).toBe('system');
                expect(last.content).toContain('150');
                expect(last.content).toContain('30');
            });
        });

        describe('vm_upgrade.needed', () => {
            it('should add system message about VM upgrade', async () => {
                const {ws} = await connectService(service);
                fireWsMessage(ws, {
                    method: 'vm_upgrade.needed',
                    params: {reason: 'sudo detected'},
                });

                const msgs = service.messages();
                const last = msgs[msgs.length - 1];
                expect(last.role).toBe('system');
                expect(last.content).toContain('sudo detected');
            });
        });

        describe('vm_upgrade.started', () => {
            it('should add system message about upgrade in progress', async () => {
                const {ws} = await connectService(service);
                fireWsMessage(ws, {method: 'vm_upgrade.started', params: {}});

                const msgs = service.messages();
                const last = msgs[msgs.length - 1];
                expect(last.role).toBe('system');
                expect(last.content).toContain('Upgrading');
            });
        });

        describe('vm_upgrade.complete', () => {
            it('should add system message about upgrade completion', async () => {
                const {ws} = await connectService(service);
                fireWsMessage(ws, {method: 'vm_upgrade.complete', params: {}});

                const msgs = service.messages();
                const last = msgs[msgs.length - 1];
                expect(last.role).toBe('system');
                expect(last.content).toContain('VM upgrade complete');
            });
        });

        describe('vm_upgrade.failed', () => {
            it('should add system message with failure reason', async () => {
                const {ws} = await connectService(service);
                fireWsMessage(ws, {
                    method: 'vm_upgrade.failed',
                    params: {reason: 'no capacity'},
                });

                const msgs = service.messages();
                const last = msgs[msgs.length - 1];
                expect(last.role).toBe('system');
                expect(last.content).toContain('no capacity');
            });
        });
    });

    // =========================================================================
    // 9.1.11: WS connection events
    // =========================================================================

    describe('WS connection events', () => {
        it('should set error state on WS error', async () => {
            const {ws} = await connectService(service);
            if (ws.onerror) ws.onerror();

            expect(service.connectionState()).toBe('error');
            expect(service.error()).toBe('WebSocket connection failed');
        });

        it('should set disconnected on abnormal close', async () => {
            const {ws} = await connectService(service);
            if (ws.onclose) ws.onclose({code: 1006, reason: 'abnormal'});

            expect(service.connectionState()).toBe('disconnected');
            expect(service.isStreaming()).toBe(false);
        });

        it('should set error message on abnormal close', async () => {
            const {ws} = await connectService(service);
            if (ws.onclose) ws.onclose({code: 1006, reason: 'server went away'});

            expect(service.error()).toContain('server went away');
        });

        it('should NOT set error on normal close (code 1000)', async () => {
            const {ws} = await connectService(service);
            if (ws.onclose) ws.onclose({code: 1000, reason: ''});

            expect(service.error()).toBeNull();
        });

        it('should ignore malformed WS messages', async () => {
            const {ws} = await connectService(service);
            // Send non-JSON — should not throw
            if (ws.onmessage) ws.onmessage({data: 'not json'});

            expect(service.error()).toBeNull();
        });
    });

    // =========================================================================
    // 9.1.12: finalizeStreaming()
    // =========================================================================

    describe('finalizeStreaming()', () => {
        it('should move streaming text to messages', async () => {
            const {ws} = await connectService(service);

            fireWsMessage(ws, {method: 'turn.started', params: {turn_id: 1}});
            fireWsMessage(ws, {method: 'token', params: {content: 'Answer text'}});
            fireWsMessage(ws, {method: 'turn.completed', params: {}});

            const msgs = service.messages();
            const last = msgs[msgs.length - 1];
            expect(last.content).toBe('Answer text');
            expect(service.streamingText()).toBe('');
        });

        it('should include tool calls in finalized message', async () => {
            const {ws} = await connectService(service);

            fireWsMessage(ws, {method: 'turn.started', params: {turn_id: 1}});
            fireWsMessage(ws, {method: 'token', params: {content: 'Used a tool'}});
            fireWsMessage(ws, {
                method: 'tool.started',
                params: {id: 'tc1', tool: 'web_search', args: {}},
            });
            fireWsMessage(ws, {
                method: 'tool.completed',
                params: {id: 'tc1', result: 'ok'},
            });
            fireWsMessage(ws, {method: 'turn.completed', params: {}});

            const msgs = service.messages();
            const last = msgs[msgs.length - 1];
            expect(last.toolCalls).toBeDefined();
            expect(last.toolCalls!.length).toBe(1);
        });

        it('should not add empty message when no text or tools', async () => {
            const {ws} = await connectService(service);
            const countBefore = service.messages().length;

            fireWsMessage(ws, {method: 'turn.started', params: {turn_id: 1}});
            fireWsMessage(ws, {method: 'turn.completed', params: {}});

            expect(service.messages().length).toBe(countBefore);
        });
    });

    // =========================================================================
    // 9.1.13: connectionState transitions
    // =========================================================================

    describe('connectionState transitions', () => {
        it('disconnected → connecting → connected', async () => {
            expect(service.connectionState()).toBe('disconnected');

            const ws = createMockWs();
            (globalThis as any).WebSocket = function MockWebSocket() {
                return ws;
            } as any;
            (globalThis as any).WebSocket.OPEN = 1;

            const promise = service.connect('tid');
            await promise;

            expect(service.connectionState()).toBe('connecting');

            if (ws.onopen) ws.onopen({});
            expect(service.connectionState()).toBe('connected');
        });

        it('connected → disconnected on close', async () => {
            const {ws} = await connectService(service);
            expect(service.connectionState()).toBe('connected');

            if (ws.onclose) ws.onclose({code: 1000, reason: ''});
            expect(service.connectionState()).toBe('disconnected');
        });

        it('connected → error on WS error', async () => {
            const {ws} = await connectService(service);
            if (ws.onerror) ws.onerror();
            expect(service.connectionState()).toBe('error');
        });
    });

    // =========================================================================
    // Session pause and resume
    // =========================================================================

    describe('session.idle_timeout', () => {
        it('should set isSessionPaused and add system message', async () => {
            const {ws} = await connectService(service);
            fireWsMessage(ws, {method: 'session.idle_timeout', params: {timeout_minutes: 15}});

            expect(service.isSessionPaused()).toBe(true);
            const msgs = service.messages();
            const last = msgs[msgs.length - 1];
            expect(last.role).toBe('system');
            expect(last.content).toContain('15 minutes');
        });

        it('should stop waiting for input', async () => {
            const {ws} = await connectService(service);
            service.isWaitingForInput.set(true);
            fireWsMessage(ws, {method: 'session.idle_timeout', params: {}});

            expect(service.isWaitingForInput()).toBe(false);
        });
    });

    describe('resumeSession()', () => {
        it('should POST to resume endpoint and reconnect', async () => {
            await connectService(service);
            // Set paused state
            service.isSessionPaused.set(true);

            mockHttp.post.mockReturnValue(of({}));
            mockHttp.get.mockReturnValue(of({messages: [], total: 0}));

            await service.resumeSession();

            expect(service.isSessionPaused()).toBe(false);
            // Verify POST was called with the resume URL
            const postCalls = mockHttp.post.mock.calls;
            const resumeCall = postCalls.find((c: any[]) => c[0].includes('/resume'));
            expect(resumeCall).toBeTruthy();
            expect(resumeCall[0]).toContain('/persistent/threads/test-thread-123/resume');
        });

        it('should do nothing if no threadId', async () => {
            // Service not connected — threadId is null
            await service.resumeSession();
            // No HTTP calls should have been made for resume
            const postCalls = mockHttp.post.mock.calls;
            const resumeCall = postCalls.find((c: any[]) => c[0]?.includes('/resume'));
            expect(resumeCall).toBeUndefined();
        });
    });

    describe('connect() resets isSessionPaused', () => {
        it('should reset isSessionPaused to false on connect', async () => {
            service.isSessionPaused.set(true);
            await connectService(service);
            expect(service.isSessionPaused()).toBe(false);
        });
    });
});
