import {describe, expect, it} from 'vitest';
import {
  collaboraHostPostmessageReady,
  parseCollaboraLoadingStatus,
} from './canvas-office-protocol';

const frameReady = {
  MessageId: 'App_LoadingStatus',
  SendTime: 1_721_822_400_000,
  Values: {
    Status: 'Frame_Ready',
    Features: {VersionStates: true},
  },
};

const documentLoaded = {
  MessageId: 'App_LoadingStatus',
  SendTime: 1_721_822_401_000,
  Values: {
    Status: 'Document_Loaded',
    DocumentLoadedTime: 1_721_822_400_900,
  },
};

describe('Collabora Canvas postMessage protocol', () => {
  it('accepts only exact loading envelopes and requires VersionStates support', () => {
    expect(parseCollaboraLoadingStatus(frameReady)).toEqual({
      status: 'Frame_Ready',
      versionStates: true,
    });
    expect(parseCollaboraLoadingStatus(JSON.stringify(documentLoaded))).toEqual({
      status: 'Document_Loaded',
    });

    for (const rejected of [
      {...frameReady, extra: true},
      {...frameReady, MessageId: 'Other'},
      {...frameReady, SendTime: 'now'},
      {...frameReady, Values: {...frameReady.Values, extra: true}},
      {...frameReady, Values: {...frameReady.Values, Features: {}}},
      {...frameReady, Values: {...frameReady.Values, Features: {VersionStates: false}}},
      {...frameReady, Values: {
        ...frameReady.Values,
        Features: {VersionStates: true, FutureFeature: true},
      }},
      {...documentLoaded, Values: {...documentLoaded.Values, extra: true}},
      {...documentLoaded, Values: {Status: 'Document_Loaded'}},
      null,
      [],
      '{malformed',
      'x'.repeat(16_385),
    ]) {
      expect(parseCollaboraLoadingStatus(rejected)).toBeNull();
    }
  });

  it('builds the exact host-ready envelope', () => {
    expect(collaboraHostPostmessageReady(1_721_822_402_000)).toEqual({
      MessageId: 'Host_PostmessageReady',
      SendTime: 1_721_822_402_000,
      Values: {},
    });
  });
});
