export interface CollaboraFrameReady {
  readonly status: 'Frame_Ready';
  readonly versionStates: boolean;
}

export interface CollaboraDocumentLoaded {
  readonly status: 'Document_Loaded';
}

export type CollaboraLoadingStatus = CollaboraFrameReady | CollaboraDocumentLoaded;

export type CollaboraMessage =
  | {readonly kind: 'loading'; readonly value: CollaboraLoadingStatus}
  | {readonly kind: 'modified'; readonly modified: boolean}
  | {
      readonly kind: 'save-response';
      readonly success: boolean;
      readonly result?: string;
    }
  | {readonly kind: 'version-restore'; readonly status: 'Pre_Restore_Ack'}
  | {readonly kind: 'token-expiring'; readonly timeout: number};

const MAX_COLLABORA_MESSAGE_CHARS = 16_384;

interface CollaboraHostMessage<TMessageId extends string, TValues> {
  readonly MessageId: TMessageId;
  readonly SendTime: number;
  readonly Values: TValues;
}

/** Parse the closed set of Collabora messages used by the Office Canvas. */
export function parseCollaboraMessage(value: unknown): CollaboraMessage | null {
  const envelope = parseEnvelope(value);
  if (!envelope) return null;
  const {messageId, values} = envelope;

  if (messageId === 'App_LoadingStatus') {
    const loading = parseLoadingValues(values);
    return loading ? {kind: 'loading', value: loading} : null;
  }
  if (
    messageId === 'Doc_ModifiedStatus' &&
    isExactRecord(values, ['Modified']) &&
    typeof values['Modified'] === 'boolean'
  ) {
    return {kind: 'modified', modified: values['Modified']};
  }
  if (messageId === 'Action_Save_Resp') {
    return parseSaveResponse(values);
  }
  if (
    messageId === 'App_VersionRestore' &&
    isExactRecord(values, ['Status']) &&
    values['Status'] === 'Pre_Restore_Ack'
  ) {
    return {kind: 'version-restore', status: 'Pre_Restore_Ack'};
  }
  if (
    messageId === 'App_TokenExpiring' &&
    isExactRecord(values, ['Timeout']) &&
    isFiniteNumber(values['Timeout']) &&
    values['Timeout'] >= 0
  ) {
    return {kind: 'token-expiring', timeout: values['Timeout']};
  }
  return null;
}

/** Compatibility surface retained for the Slice 1 loading-handshake tests. */
export function parseCollaboraLoadingStatus(value: unknown): CollaboraLoadingStatus | null {
  const message = parseCollaboraMessage(value);
  return message?.kind === 'loading' ? message.value : null;
}

export function collaboraHostPostmessageReady(
  sendTime = Date.now(),
): CollaboraHostMessage<'Host_PostmessageReady', Record<string, never>> {
  return hostMessage('Host_PostmessageReady', {}, sendTime);
}

export function collaboraActionSave(
  sendTime = Date.now(),
): CollaboraHostMessage<
  'Action_Save',
  {readonly DontSaveIfUnmodified: true; readonly Notify: true}
> {
  return hostMessage(
    'Action_Save',
    {DontSaveIfUnmodified: true, Notify: true},
    sendTime,
  );
}

export function collaboraHostVersionRestore(
  sendTime = Date.now(),
): CollaboraHostMessage<
  'Host_VersionRestore',
  {readonly Status: 'Pre_Restore'}
> {
  return hostMessage(
    'Host_VersionRestore',
    {Status: 'Pre_Restore'},
    sendTime,
  );
}

export function collaboraResetAccessToken(
  token: string,
  ttl: number,
  sendTime = Date.now(),
): CollaboraHostMessage<
  'Reset_Access_Token',
  {readonly token: string; readonly ttl: number}
> {
  return hostMessage('Reset_Access_Token', {token, ttl}, sendTime);
}

function parseEnvelope(
  value: unknown,
): {readonly messageId: string; readonly values: unknown} | null {
  if (typeof value === 'string') {
    if (!value || value.length > MAX_COLLABORA_MESSAGE_CHARS) return null;
    try {
      value = JSON.parse(value) as unknown;
    } catch {
      return null;
    }
  }
  if (
    !isExactRecord(value, ['MessageId', 'SendTime', 'Values']) ||
    typeof value['MessageId'] !== 'string' ||
    !isFiniteNumber(value['SendTime'])
  ) {
    return null;
  }
  return {messageId: value['MessageId'], values: value['Values']};
}

function parseLoadingValues(values: unknown): CollaboraLoadingStatus | null {
  if (
    isExactRecord(values, ['Features', 'Status']) &&
    values['Status'] === 'Frame_Ready'
  ) {
    const features = values['Features'];
    if (isExactRecord(features, [])) {
      return {status: 'Frame_Ready', versionStates: false};
    }
    return isExactRecord(features, ['VersionStates']) &&
      features['VersionStates'] === true
      ? {status: 'Frame_Ready', versionStates: true}
      : null;
  }
  if (
    isExactRecord(values, ['DocumentLoadedTime', 'Status']) &&
    values['Status'] === 'Document_Loaded' &&
    isFiniteNumber(values['DocumentLoadedTime'])
  ) {
    return {status: 'Document_Loaded'};
  }
  return null;
}

function parseSaveResponse(values: unknown): CollaboraMessage | null {
  if (!isSaveResponseShape(values) || typeof values['success'] !== 'boolean') {
    return null;
  }
  const result = values['result'];
  if (result !== undefined && typeof result !== 'string') return null;
  if (
    values['errorMsg'] !== undefined &&
    typeof values['errorMsg'] !== 'string'
  ) {
    return null;
  }
  if (values['cmd'] !== undefined && typeof values['cmd'] !== 'string') {
    return null;
  }
  return result === undefined
    ? {kind: 'save-response', success: values['success']}
    : {kind: 'save-response', success: values['success'], result};
}

function isSaveResponseShape(value: unknown): value is Record<string, unknown> {
  return (
    isExactRecord(value, ['success']) ||
    isExactRecord(value, ['result', 'success']) ||
    isExactRecord(value, ['errorMsg', 'result', 'success']) ||
    isExactRecord(value, ['cmd', 'errorMsg', 'result', 'success'])
  );
}

function hostMessage<TMessageId extends string, TValues>(
  MessageId: TMessageId,
  Values: TValues,
  SendTime: number,
): CollaboraHostMessage<TMessageId, TValues> {
  return {MessageId, SendTime, Values};
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

function isExactRecord(
  value: unknown,
  expectedKeys: readonly string[],
): value is Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false;
  const keys = Object.keys(value).sort();
  const expected = [...expectedKeys].sort();
  return keys.length === expected.length && keys.every((key, index) => key === expected[index]);
}
