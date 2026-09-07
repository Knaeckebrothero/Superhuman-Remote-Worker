import {HttpErrorResponse} from '@angular/common/http';
import {firstValueFrom, Observable} from 'rxjs';
import {environment} from '../environment';
import type {OfficerPost} from '../models/api.model';

/**
 * The officer conference's trusted thread-create request. The body carries
 * only the conference flag — the server inherits his brain (model +
 * reasoning) from the standing post and enforces the owner role, so no
 * client may claim either (officer_visibility_streamline.md §3.1/§3.5).
 */
export function buildConferenceThreadCreateBody(
  projectId: string,
  projectName: string,
  conferenceLabel: string,
): Record<string, unknown> {
  return {
    title: `${conferenceLabel} — ${projectName}`,
    config_name: 'centurion',
    project_ids: [projectId],
    use_datasource_defaults: true,
    config_override: {officer: {conference: true}},
  };
}

/** Router commands for the launcher route: `/projects/:id/officer/conference`. */
export function conferenceLauncherCommands(projectId: string): string[] {
  return ['/projects', projectId, 'officer', 'conference'];
}

export type ConferenceOpenResult =
  | {kind: 'opened'; threadId: string; resumed: boolean}
  | {kind: 'error'; status: number; detail: string};

export interface ConferenceOpenDeps {
  http: {post<T>(url: string, body: unknown): Observable<T>};
  getOfficerPost: (projectId: string) => Observable<OfficerPost | null>;
}

function detailOf(err: unknown): string {
  const detail = (err as {error?: {detail?: unknown}})?.error?.detail;
  return typeof detail === 'string' ? detail : '';
}

async function openConferenceId(deps: ConferenceOpenDeps, projectId: string): Promise<string | null> {
  const post = await firstValueFrom(deps.getOfficerPost(projectId)).catch(() => null);
  return post?.conference?.thread_id ?? null;
}

/**
 * The one create-or-resume path (§3.5): resume the project's open conference
 * if there is one, else create it; a lost race (`conference_open` 409) is a
 * resume, not an error. Never returns a half-created thread — the server
 * refuses before provisioning.
 */
export async function openOrResumeConference(
  deps: ConferenceOpenDeps,
  projectId: string,
  projectName: string,
  conferenceLabel: string,
): Promise<ConferenceOpenResult> {
  const existing = await openConferenceId(deps, projectId);
  if (existing) return {kind: 'opened', threadId: existing, resumed: true};
  try {
    const resp = await firstValueFrom(
      deps.http.post<{thread_id: string}>(
        `${environment.apiUrl}/persistent/threads`,
        buildConferenceThreadCreateBody(projectId, projectName, conferenceLabel),
      ),
    );
    return {kind: 'opened', threadId: resp.thread_id, resumed: false};
  } catch (err) {
    const status = (err as HttpErrorResponse)?.status ?? 0;
    const detail = detailOf(err);
    if (status === 409 && detail.startsWith('conference_open')) {
      const raced = await openConferenceId(deps, projectId);
      if (raced) return {kind: 'opened', threadId: raced, resumed: true};
    }
    return {kind: 'error', status, detail};
  }
}
