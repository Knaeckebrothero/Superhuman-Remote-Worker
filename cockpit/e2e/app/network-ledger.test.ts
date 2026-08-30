import type { ConsoleMessage, Page, Request, Response } from '@playwright/test';
import { describe, expect, it } from 'vitest';
import { NetworkLedger, sanitizedDiagnostic } from './network-ledger';

type PageEvent = 'request' | 'response' | 'requestfailed' | 'pageerror' | 'console';
type PageHandler = (value: unknown) => void;

function ledgerHarness(): {
  ledger: NetworkLedger;
  handlers: Map<PageEvent, PageHandler>;
} {
  const handlers = new Map<PageEvent, PageHandler>();
  const page = {
    on: (event: PageEvent, handler: PageHandler): void => {
      handlers.set(event, handler);
    },
  } as unknown as Page;
  return {
    ledger: new NetworkLedger(page, 'http://srw-e2e.test'),
    handlers,
  };
}

function emitConsole(
  handlers: Map<PageEvent, PageHandler>,
  message: string,
  locationUrl = '',
  type: 'error' | 'warning' = 'error',
): void {
  handlers.get('console')?.({
    type: () => type,
    text: () => message,
    location: () => ({ url: locationUrl }),
  } as ConsoleMessage);
}

function emitRequestFailed(
  handlers: Map<PageEvent, PageHandler>,
  method: string,
  url: string,
  failure = 'net::ERR_ABORTED',
): void {
  handlers.get('requestfailed')?.({
    method: () => method,
    url: () => url,
    failure: () => ({ errorText: failure }),
  } as Request);
}

function emitResponse(
  handlers: Map<PageEvent, PageHandler>,
  method: string,
  url: string,
  status: number,
): void {
  const request = {
    method: () => method,
    url: () => url,
  } as Request;
  handlers.get('request')?.(request);
  handlers.get('response')?.({
    request: () => request,
    url: () => url,
    status: () => status,
  } as Response);
}

describe('network ledger safety and warm-up classification', () => {
  it('redacts websocket query credentials before truncating diagnostics', () => {
    const jwt = `${'a'.repeat(24)}.${'b'.repeat(24)}.${'c'.repeat(24)}`;
    const diagnostic = sanitizedDiagnostic(
      `WebSocket connection to 'wss://srw-e2e.test/p/thread/ws?t=${jwt}' failed`,
    );

    expect(diagnostic).toContain('wss://srw-e2e.test/p/thread/ws');
    expect(diagnostic).not.toContain(jwt);
    expect(diagnostic).not.toContain('?t=');
  });

  it('allows only the documented connection response during warm-up', () => {
    const { ledger, handlers } = ledgerHarness();
    ledger.registerThread('thread-id');
    ledger.setPhase('turn');
    emitResponse(handlers, 'GET', 'http://srw-e2e.test/api/sessions/thread-id/connection', 425);
    emitConsole(
      handlers,
      'Failed to load resource: the server responded with a status of 425 (Too Early)',
      'http://srw-e2e.test/api/sessions/thread-id/connection',
    );

    expect(ledger.problems()).toEqual([]);
  });

  it('rejects a warm-up response for a foreign thread', () => {
    const { ledger, handlers } = ledgerHarness();
    ledger.registerThread('owned-thread');
    ledger.setPhase('turn');
    emitResponse(handlers, 'GET', 'http://srw-e2e.test/api/sessions/foreign/connection', 425);
    emitConsole(
      handlers,
      'Failed to load resource: the server responded with a status of 425 (Too Early)',
      'http://srw-e2e.test/api/sessions/foreign/connection',
    );

    expect(ledger.problems()).toHaveLength(2);
  });

  it('rejects an owned-thread warm-up console error from a foreign origin', () => {
    const { ledger, handlers } = ledgerHarness();
    ledger.registerThread('owned-thread');
    ledger.setPhase('turn');
    emitConsole(
      handlers,
      'Failed to load resource: the server responded with a status of 425 (Too Early)',
      'http://foreign.test/api/sessions/owned-thread/connection',
    );

    expect(ledger.problems()).toHaveLength(1);
  });

  it('rejects a 425 response and console error on an unrelated endpoint', () => {
    const { ledger, handlers } = ledgerHarness();
    ledger.setPhase('turn');
    emitResponse(handlers, 'POST', 'http://srw-e2e.test/api/persistent/threads/x/input', 425);
    emitConsole(
      handlers,
      'Failed to load resource: the server responded with a status of 425 (Too Early)',
      'http://srw-e2e.test/api/persistent/threads/x/input',
    );

    expect(ledger.problems()).toHaveLength(2);
  });

  it('allows a failed optional control socket only for the owned thread', () => {
    const { ledger, handlers } = ledgerHarness();
    ledger.registerThread('owned-thread');
    ledger.setPhase('turn');
    emitConsole(
      handlers,
      "WebSocket connection to 'wss://srw-e2e.test/p/owned-thread/ws?t=secret' failed",
    );

    expect(ledger.problems()).toEqual([]);

    emitConsole(
      handlers,
      "WebSocket connection to 'wss://srw-e2e.test/p/foreign-thread/ws?t=secret' failed",
    );
    expect(ledger.problems()).toHaveLength(1);

    emitConsole(
      handlers,
      "WebSocket connection to 'wss://foreign.test/p/owned-thread/ws?t=secret' failed",
    );
    expect(ledger.problems()).toHaveLength(2);
  });

  it('allows only the exact owned-stream abort armed by the warm-up watchdog', () => {
    const { ledger, handlers } = ledgerHarness();
    ledger.registerThread('owned-thread');
    ledger.setPhase('turn');
    emitConsole(
      handlers,
      '[persistent-chat] no SSE data within 5000ms of send — forcing reconnect',
      'http://srw-e2e.test/main.js',
      'warning',
    );
    emitRequestFailed(
      handlers,
      'GET',
      'http://srw-e2e.test/api/persistent/threads/owned-thread/stream',
    );

    expect(ledger.problems()).toEqual([]);

    emitRequestFailed(
      handlers,
      'GET',
      'http://srw-e2e.test/api/persistent/threads/owned-thread/stream',
    );
    expect(ledger.problems()).toHaveLength(1);
  });

  it('does not spend an armed reconnect on a foreign thread stream', () => {
    const { ledger, handlers } = ledgerHarness();
    ledger.registerThread('owned-thread');
    ledger.setPhase('turn');
    emitConsole(
      handlers,
      '[persistent-chat] no SSE data within 5000ms of send — forcing reconnect',
      'http://srw-e2e.test/main.js',
      'warning',
    );
    emitRequestFailed(
      handlers,
      'GET',
      'http://srw-e2e.test/api/persistent/threads/foreign-thread/stream',
    );
    emitRequestFailed(
      handlers,
      'GET',
      'http://srw-e2e.test/api/persistent/threads/owned-thread/stream',
    );

    expect(ledger.problems()).toHaveLength(1);
  });
});
