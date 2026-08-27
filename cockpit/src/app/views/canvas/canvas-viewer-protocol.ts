import {CanvasViewAuthorization} from '../../core/models/canvas.model';

export const CANVAS_BOOTSTRAP_CHANNEL = 'srw.canvas.bootstrap' as const;
export const CANVAS_BOOTSTRAP_VERSION = 1 as const;

const CANONICAL_UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const OPAQUE_VALUE_PATTERN = /^[A-Za-z0-9_-]{32,128}$/;
const ERROR_CODE_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;

interface CanvasBootstrapEnvelope {
  readonly channel: typeof CANVAS_BOOTSTRAP_CHANNEL;
  readonly version: typeof CANVAS_BOOTSTRAP_VERSION;
  readonly attachment_id: string;
  readonly challenge: string;
  readonly ready_receipt: string;
}

export interface CanvasBootstrapChallengeMessage extends CanvasBootstrapEnvelope {
  readonly type: 'challenge';
}

export interface CanvasBootstrapReadyMessage extends CanvasBootstrapEnvelope {
  readonly type: 'ready';
}

export interface CanvasBootstrapErrorMessage extends CanvasBootstrapEnvelope {
  readonly type: 'error';
  readonly code: string;
}

export type CanvasBootstrapMessage =
  | CanvasBootstrapChallengeMessage
  | CanvasBootstrapReadyMessage
  | CanvasBootstrapErrorMessage;

export interface CanvasBootstrapAuthorizeMessage {
  readonly channel: typeof CANVAS_BOOTSTRAP_CHANNEL;
  readonly version: typeof CANVAS_BOOTSTRAP_VERSION;
  readonly type: 'authorize';
  readonly attachment_id: string;
  readonly challenge: string;
  readonly exchange_code: string;
}

export function isCanonicalCanvasUuid(value: unknown): value is string {
  return typeof value === 'string' && CANONICAL_UUID_PATTERN.test(value);
}

export function isCanvasOpaqueValue(value: unknown): value is string {
  return typeof value === 'string' && OPAQUE_VALUE_PATTERN.test(value);
}

/** Parse only the fixed bootstrap protocol; additional properties fail closed. */
export function parseCanvasBootstrapMessage(value: unknown): CanvasBootstrapMessage | null {
  if (!isRecord(value)) return null;
  if (
    value['channel'] !== CANVAS_BOOTSTRAP_CHANNEL ||
    value['version'] !== CANVAS_BOOTSTRAP_VERSION ||
    !isCanonicalCanvasUuid(value['attachment_id']) ||
    !isCanvasOpaqueValue(value['challenge']) ||
    !isCanvasOpaqueValue(value['ready_receipt'])
  ) {
    return null;
  }

  const type = value['type'];
  if (type === 'challenge' || type === 'ready') {
    if (!hasExactKeys(value, [
      'attachment_id', 'challenge', 'channel', 'ready_receipt', 'type', 'version',
    ])) {
      return null;
    }
    return value as unknown as CanvasBootstrapChallengeMessage | CanvasBootstrapReadyMessage;
  }
  if (
    type === 'error' &&
    typeof value['code'] === 'string' &&
    ERROR_CODE_PATTERN.test(value['code']) &&
    hasExactKeys(value, [
      'attachment_id', 'challenge', 'channel', 'code', 'ready_receipt', 'type', 'version',
    ])
  ) {
    return value as unknown as CanvasBootstrapErrorMessage;
  }
  return null;
}

/** Validate the BFF response before any value is posted across the iframe boundary. */
export function parseCanvasViewAuthorization(value: unknown): CanvasViewAuthorization | null {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, ['challenge', 'exchange_code', 'expires_at', 'ready_receipt']) ||
    !isCanvasOpaqueValue(value['challenge']) ||
    !isCanvasOpaqueValue(value['ready_receipt']) ||
    !isCanvasOpaqueValue(value['exchange_code']) ||
    typeof value['expires_at'] !== 'string'
  ) {
    return null;
  }
  return value as unknown as CanvasViewAuthorization;
}

export function canvasBootstrapAuthorizeMessage(
  attachmentId: string,
  challenge: string,
  exchangeCode: string,
): CanvasBootstrapAuthorizeMessage {
  return {
    channel: CANVAS_BOOTSTRAP_CHANNEL,
    version: CANVAS_BOOTSTRAP_VERSION,
    type: 'authorize',
    attachment_id: attachmentId,
    challenge,
    exchange_code: exchangeCode,
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function hasExactKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
  const keys = Object.keys(value).sort();
  return keys.length === expected.length && keys.every((key, index) => key === expected[index]);
}
