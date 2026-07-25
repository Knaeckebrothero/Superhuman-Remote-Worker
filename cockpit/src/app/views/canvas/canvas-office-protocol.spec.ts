import {describe, expect, it} from 'vitest';
import {
  collaboraActionSave,
  collaboraHostVersionRestore,
  collaboraHostPostmessageReady,
  collaboraResetAccessToken,
  parseCollaboraMessage,
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
  it('accepts only exact loading envelopes and feature-detects VersionStates', () => {
    expect(parseCollaboraLoadingStatus(frameReady)).toEqual({
      status: 'Frame_Ready',
      versionStates: true,
    });
    expect(parseCollaboraLoadingStatus({
      ...frameReady,
      Values: {Status: 'Frame_Ready', Features: {}},
    })).toEqual({
      status: 'Frame_Ready',
      versionStates: false,
    });
    expect(parseCollaboraLoadingStatus(JSON.stringify(documentLoaded))).toEqual({
      status: 'Document_Loaded',
    });

    for (const rejected of [
      {...frameReady, extra: true},
      {...frameReady, MessageId: 'Other'},
      {...frameReady, SendTime: 'now'},
      {...frameReady, Values: {...frameReady.Values, extra: true}},
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

  it('strictly parses editing, restore, and token lifecycle envelopes', () => {
    expect(parseCollaboraMessage({
      MessageId: 'Doc_ModifiedStatus',
      SendTime: 3,
      Values: {Modified: true},
    })).toEqual({kind: 'modified', modified: true});
    expect(parseCollaboraMessage({
      MessageId: 'Action_Save_Resp',
      SendTime: 4,
      Values: {success: false, result: 'unmodified', errorMsg: ''},
    })).toEqual({
      kind: 'save-response',
      success: false,
      result: 'unmodified',
    });
    expect(parseCollaboraMessage({
      MessageId: 'App_VersionRestore',
      SendTime: 5,
      Values: {Status: 'Pre_Restore_Ack'},
    })).toEqual({kind: 'version-restore', status: 'Pre_Restore_Ack'});
    expect(parseCollaboraMessage({
      MessageId: 'App_TokenExpiring',
      SendTime: 6,
      Values: {Timeout: 899_000},
    })).toEqual({kind: 'token-expiring', timeout: 899_000});

    for (const rejected of [
      {
        MessageId: 'Doc_ModifiedStatus',
        SendTime: 3,
        Values: {Modified: true, extra: false},
      },
      {
        MessageId: 'Action_Save_Resp',
        SendTime: 4,
        Values: {success: true, unexpected: true},
      },
      {
        MessageId: 'App_VersionRestore',
        SendTime: 5,
        Values: {Status: 'Wrong'},
      },
      {
        MessageId: 'App_TokenExpiring',
        SendTime: 6,
        Values: {Timeout: 'soon'},
      },
    ]) {
      expect(parseCollaboraMessage(rejected)).toBeNull();
    }
  });

  it('builds exact host envelopes for ready, save, restore, and renewal', () => {
    expect(collaboraHostPostmessageReady(1_721_822_402_000)).toEqual({
      MessageId: 'Host_PostmessageReady',
      SendTime: 1_721_822_402_000,
      Values: {},
    });
    expect(collaboraActionSave(1_721_822_403_000)).toEqual({
      MessageId: 'Action_Save',
      SendTime: 1_721_822_403_000,
      Values: {
        DontSaveIfUnmodified: true,
        Notify: true,
      },
    });
    expect(collaboraHostVersionRestore(1_721_822_404_000)).toEqual({
      MessageId: 'Host_VersionRestore',
      SendTime: 1_721_822_404_000,
      Values: {Status: 'Pre_Restore'},
    });
    expect(collaboraResetAccessToken(
      'renewed-token',
      1_721_858_400_000,
      1_721_822_405_000,
    )).toEqual({
      MessageId: 'Reset_Access_Token',
      SendTime: 1_721_822_405_000,
      Values: {
        token: 'renewed-token',
        ttl: 1_721_858_400_000,
      },
    });
  });
});
