import {describe, expect, it, vi} from 'vitest';
import {HttpErrorResponse} from '@angular/common/http';
import {of, throwError} from 'rxjs';
import {
  buildConferenceThreadCreateBody,
  conferenceLauncherCommands,
  openOrResumeConference,
} from './conference';

function deps(opts: {post?: unknown; getPost?: unknown} = {}) {
  return {
    http: {post: vi.fn().mockReturnValue(opts.post ?? of({thread_id: 'new-conf'}))},
    getOfficerPost: vi.fn().mockReturnValue(opts.getPost ?? of({conference: null})),
  };
}

const conflict = () =>
  throwError(
    () =>
      new HttpErrorResponse({
        status: 409,
        error: {detail: 'conference_open: this project already has an open conference session (old-conf)'},
      }),
  );

describe('buildConferenceThreadCreateBody', () => {
  it('requests connector defaults and the conference flag only', () => {
    expect(buildConferenceThreadCreateBody('project-1', 'Apollo', 'Conference')).toEqual({
      title: 'Conference — Apollo',
      config_name: 'centurion',
      project_ids: ['project-1'],
      use_datasource_defaults: true,
      config_override: {officer: {conference: true}},
    });
  });
});

describe('conferenceLauncherCommands', () => {
  it('addresses the launcher route', () => {
    expect(conferenceLauncherCommands('p1')).toEqual(['/projects', 'p1', 'officer', 'conference']);
  });
});

describe('openOrResumeConference', () => {
  it('resumes the open conference without creating a second one', async () => {
    const d = deps({getPost: of({conference: {thread_id: 'old-conf'}})});
    const r = await openOrResumeConference(d, 'p1', 'Apollo', 'Conference');
    expect(r).toEqual({kind: 'opened', threadId: 'old-conf', resumed: true});
    expect(d.http.post).not.toHaveBeenCalled();
  });

  it('creates one when none is open and lands on the new thread', async () => {
    const d = deps();
    const r = await openOrResumeConference(d, 'p1', 'Apollo', 'Conference');
    expect(r).toEqual({kind: 'opened', threadId: 'new-conf', resumed: false});
    expect(d.http.post).toHaveBeenCalledWith(
      expect.stringMatching(/\/persistent\/threads$/),
      buildConferenceThreadCreateBody('p1', 'Apollo', 'Conference'),
    );
  });

  it('turns a lost race (409 conference_open) into a resume', async () => {
    const getPost = vi
      .fn()
      .mockReturnValueOnce(of({conference: null}))
      .mockReturnValueOnce(of({conference: {thread_id: 'raced-conf'}}));
    const d = {http: {post: vi.fn().mockReturnValue(conflict())}, getOfficerPost: getPost};
    const r = await openOrResumeConference(d, 'p1', 'Apollo', 'Conference');
    expect(r).toEqual({kind: 'opened', threadId: 'raced-conf', resumed: true});
  });

  it('reports other failures with the server detail', async () => {
    const d = deps({
      post: throwError(
        () => new HttpErrorResponse({status: 403, error: {detail: 'Project owner role required'}}),
      ),
    });
    const r = await openOrResumeConference(d, 'p1', 'Apollo', 'Conference');
    expect(r).toEqual({kind: 'error', status: 403, detail: 'Project owner role required'});
  });

  it('survives a failing post read and still tries to create', async () => {
    const d = deps({getPost: throwError(() => new Error('boom'))});
    const r = await openOrResumeConference(d, 'p1', 'Apollo', 'Conference');
    expect(r).toEqual({kind: 'opened', threadId: 'new-conf', resumed: false});
  });
});
