import {describe, expect, it} from 'vitest';
import {
  CANVAS_BOOTSTRAP_CHANNEL,
  CANVAS_BOOTSTRAP_VERSION,
  canvasBootstrapAuthorizeMessage,
  isCanonicalCanvasUuid,
  parseCanvasBootstrapMessage,
  parseCanvasViewAuthorization,
} from './canvas-viewer-protocol';

const ATTACHMENT_ID = '54f4fd56-69d8-46c8-8ab7-a3349af0d784';
const CHALLENGE = 'c'.repeat(43);
const RECEIPT = 'r'.repeat(43);
const EXCHANGE_CODE = 'e'.repeat(43);

function message(type: 'challenge' | 'ready' = 'challenge') {
  return {
    channel: CANVAS_BOOTSTRAP_CHANNEL,
    version: CANVAS_BOOTSTRAP_VERSION,
    type,
    attachment_id: ATTACHMENT_ID,
    challenge: CHALLENGE,
    ready_receipt: RECEIPT,
  };
}

describe('Canvas viewer bootstrap protocol', () => {
  it('accepts only canonical UUIDs and exact challenge/ready schemas', () => {
    expect(isCanonicalCanvasUuid(ATTACHMENT_ID)).toBe(true);
    expect(isCanonicalCanvasUuid(ATTACHMENT_ID.toUpperCase())).toBe(false);
    expect(parseCanvasBootstrapMessage(message())).toEqual(message());
    expect(parseCanvasBootstrapMessage(message('ready'))).toEqual(message('ready'));

    for (const rejected of [
      {...message(), extra: true},
      {...message(), channel: 'srw.canvas.other'},
      {...message(), version: 2},
      {...message(), attachment_id: 'not-a-uuid'},
      {...message(), challenge: 'short'},
      {...message(), ready_receipt: 'short'},
      {...message(), type: 'authorize'},
      null,
      [],
    ]) {
      expect(parseCanvasBootstrapMessage(rejected)).toBeNull();
    }
  });

  it('accepts a bounded error only with the complete correlation tuple', () => {
    const error = {...message(), type: 'error', code: 'exchange_failed'};
    expect(parseCanvasBootstrapMessage(error)).toEqual(error);
    expect(parseCanvasBootstrapMessage({...error, code: 'INVALID CODE'})).toBeNull();
    const {ready_receipt: _removed, ...incomplete} = error;
    expect(parseCanvasBootstrapMessage(incomplete)).toBeNull();
  });

  it('strictly validates authorization responses and builds a fixed parent message', () => {
    const response = {
      challenge: CHALLENGE,
      ready_receipt: RECEIPT,
      exchange_code: EXCHANGE_CODE,
      expires_at: '2026-07-13T10:00:30Z',
    };
    expect(parseCanvasViewAuthorization(response)).toEqual(response);
    expect(parseCanvasViewAuthorization({...response, extra: true})).toBeNull();
    expect(parseCanvasViewAuthorization({...response, exchange_code: 'short'})).toBeNull();

    expect(canvasBootstrapAuthorizeMessage(ATTACHMENT_ID, CHALLENGE, EXCHANGE_CODE)).toEqual({
      channel: CANVAS_BOOTSTRAP_CHANNEL,
      version: CANVAS_BOOTSTRAP_VERSION,
      type: 'authorize',
      attachment_id: ATTACHMENT_ID,
      challenge: CHALLENGE,
      exchange_code: EXCHANGE_CODE,
    });
  });
});
