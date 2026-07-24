export interface CollaboraFrameReady {
  readonly status: 'Frame_Ready';
  readonly versionStates: true;
}

export interface CollaboraDocumentLoaded {
  readonly status: 'Document_Loaded';
}

export type CollaboraLoadingStatus = CollaboraFrameReady | CollaboraDocumentLoaded;

const MAX_COLLABORA_MESSAGE_CHARS = 16_384;

interface CollaboraHostReady {
  readonly MessageId: 'Host_PostmessageReady';
  readonly SendTime: number;
  readonly Values: Record<string, never>;
}

/** Parse only the two exact Collabora loading envelopes used by Slice 1. */
export function parseCollaboraLoadingStatus(value: unknown): CollaboraLoadingStatus | null {
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
    value['MessageId'] !== 'App_LoadingStatus' ||
    !isFiniteNumber(value['SendTime'])
  ) {
    return null;
  }

  const values = value['Values'];
  if (
    isExactRecord(values, ['Features', 'Status']) &&
    values['Status'] === 'Frame_Ready'
  ) {
    const features = values['Features'];
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

export function collaboraHostPostmessageReady(sendTime = Date.now()): CollaboraHostReady {
  return {
    MessageId: 'Host_PostmessageReady',
    SendTime: sendTime,
    Values: {},
  };
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
