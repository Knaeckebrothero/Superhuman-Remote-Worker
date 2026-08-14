import { CUSTOM_ELEMENTS_SCHEMA, signal, ɵresolveComponentResources } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { TranslocoPipe, TranslocoTestingModule } from '@jsverse/transloco';
import { of } from 'rxjs';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import en from '../../../assets/i18n/en.json';
import { Datasource, Job, PullRequestStatus } from '../../core/models/api.model';
import { ApiService } from '../../core/services/api.service';
import { DataService } from '../../core/services/data.service';
import {
  JobReviewComponent,
  pullRequestFromJob,
  repositoryBranchUrl,
  repositoryWebUrl,
  selectDeliveryRepository,
} from './job-review.component';

const JOB_ID = '29c28492-df7c-4eb3-847f-38892557ac4e';
const PR_URL = 'https://github.com/Knaeckebrothero/KurortEngine/pull/1';
const OPEN_STATUS: PullRequestStatus = {
  forge: 'github',
  repo: 'Knaeckebrothero/KurortEngine',
  number: 1,
  url: PR_URL,
  state: 'open',
  head: 'design/hotel-rheinland-theme',
  base: 'main',
  draft: false,
};

function repository(id: string, connectionUrl: string): Datasource {
  return {
    id,
    name: id,
    description: null,
    type: 'repository',
    connection_url: connectionUrl,
    cli_hint: null,
    default_branch: 'main',
    config: { forge: 'github' },
    job_id: JOB_ID,
    created_at: '',
    updated_at: '',
  };
}

function reviewJob(context: Job['context']): Job {
  return {
    id: JOB_ID,
    description: 'Design the Hotel Rheinland theme',
    config_name: 'worker_base',
    status: 'pending_review',
    created_at: '2026-08-14T08:00:00Z',
    repo_name: 'job-29c28492',
    branch_name: null,
    context,
  };
}

describe('job-review delivery URL helpers', () => {
  it('turns HTTPS and SSH clone URLs into credential-free repository pages', () => {
    expect(repositoryWebUrl('https://token@github.com/acme/widget.git')).toBe(
      'https://github.com/acme/widget',
    );
    expect(repositoryWebUrl('git@github.com:acme/widget.git')).toBe(
      'https://github.com/acme/widget',
    );
    expect(repositoryWebUrl('ssh://git@git.example.test/acme/widget.git')).toBe(
      'https://git.example.test/acme/widget',
    );
  });

  it('builds forge-specific branch pages without losing slash-delimited refs', () => {
    const repoUrl = 'https://github.com/acme/widget.git';
    expect(repositoryBranchUrl(repoUrl, 'design/hotel theme', 'github')).toBe(
      'https://github.com/acme/widget/tree/design/hotel%20theme',
    );
    expect(repositoryBranchUrl(repoUrl, 'feature/a', 'gitlab')).toBe(
      'https://github.com/acme/widget/-/tree/feature/a',
    );
    expect(repositoryBranchUrl(repoUrl, 'feature/a', 'gitea')).toBe(
      'https://github.com/acme/widget/src/branch/feature/a',
    );
  });

  it('reads the persisted PR from raw JSONB text and rejects unsafe links', () => {
    const pullRequest = pullRequestFromJob(
      reviewJob(
        JSON.stringify({
          pull_request: {
            forge: 'github',
            repo: 'Knaeckebrothero/KurortEngine',
            number: 1,
            url: PR_URL,
            head: 'design/hotel-rheinland-theme',
            base: 'main',
          },
        }),
      ),
    );
    expect(pullRequest?.url).toBe(PR_URL);
    expect(pullRequest?.head).toBe('design/hotel-rheinland-theme');

    const unsafe = pullRequestFromJob(
      reviewJob({
        pull_request: {
          forge: 'github',
          repo: 'acme/widget',
          number: 1,
          url: 'javascript:alert(1)',
          head: 'feature/a',
          base: 'main',
        },
      }),
    );
    expect(unsafe).toBeNull();
  });

  it('matches the source connector to the PR instead of taking an unrelated repo', () => {
    const pullRequest = pullRequestFromJob(
      reviewJob({
        pull_request: {
          forge: 'github',
          repo: 'Knaeckebrothero/KurortEngine',
          number: 1,
          url: PR_URL,
          head: 'design/hotel-rheinland-theme',
          base: 'main',
        },
      }),
    );
    const selected = selectDeliveryRepository(
      [
        repository('unrelated', 'https://github.com/acme/other.git'),
        repository('kurort-engine', 'https://github.com/Knaeckebrothero/KurortEngine.git'),
      ],
      pullRequest,
    );
    expect(selected?.id).toBe('kurort-engine');
  });

  it('does not label an unrelated connector as the source when several are attached', () => {
    const pullRequest = pullRequestFromJob(
      reviewJob({
        pull_request: {
          forge: 'github',
          repo: 'Knaeckebrothero/KurortEngine',
          number: 1,
          url: PR_URL,
          head: 'design/hotel-rheinland-theme',
          base: 'main',
        },
      }),
    );
    expect(
      selectDeliveryRepository(
        [
          repository('first', 'https://github.com/acme/first.git'),
          repository('second', 'https://github.com/acme/second.git'),
        ],
        pullRequest,
      ),
    ).toBeNull();
  });
});

describe('JobReviewComponent delivery section', () => {
  let fixture: ComponentFixture<JobReviewComponent>;

  beforeAll(async () => {
    await ɵresolveComponentResources(() => Promise.resolve(''));
  });

  afterEach(() => TestBed.resetTestingModule());

  async function render(
    context: Job['context'] = JSON.stringify({
      pull_request: {
        forge: 'github',
        repo: 'Knaeckebrothero/KurortEngine',
        number: 1,
        url: PR_URL,
        head: 'design/hotel-rheinland-theme',
        base: 'main',
      },
    }),
    liveStatus: PullRequestStatus | null = OPEN_STATUS,
  ): Promise<HTMLElement> {
    const job = reviewJob(context);
    const api = {
      getJob: vi.fn().mockReturnValue(of(job)),
      getFrozenJobData: vi.fn().mockReturnValue(of({ summary: 'Theme delivered' })),
      getJobPullRequestStatus: vi.fn().mockReturnValue(of(liveStatus)),
      getJobDatasources: vi
        .fn()
        .mockReturnValue(
          of([repository('kurort-engine', 'https://github.com/Knaeckebrothero/KurortEngine.git')]),
        ),
      ensureWorkspaceAccess: vi.fn().mockReturnValue(of({ status: 'ready' })),
    };

    TestBed.configureTestingModule({
      imports: [
        JobReviewComponent,
        TranslocoTestingModule.forRoot({
          langs: { en },
          translocoConfig: { availableLangs: ['en'], defaultLang: 'en' },
        }),
      ],
      providers: [
        { provide: ApiService, useValue: api },
        { provide: DataService, useValue: { currentJobId: signal(JOB_ID) } },
      ],
    });
    // Signal-input metadata on the design-system children is unavailable in
    // this Vitest JIT pipeline. Keep the real host template and translation
    // pipe, while treating its child elements as inert render containers.
    TestBed.overrideComponent(JobReviewComponent, {
      set: { imports: [TranslocoPipe], schemas: [CUSTOM_ELEMENTS_SCHEMA] },
    });
    await TestBed.compileComponents();
    fixture = TestBed.createComponent(JobReviewComponent);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(api.getJobDatasources).toHaveBeenCalledWith(JOB_ID);
    if (pullRequestFromJob(job)) {
      expect(api.getJobPullRequestStatus).toHaveBeenCalledWith(JOB_ID);
    } else {
      expect(api.getJobPullRequestStatus).not.toHaveBeenCalled();
    }
    return fixture.nativeElement as HTMLElement;
  }

  it('links the real delivery and labels the scratch repository as a job workspace', async () => {
    const root = await render();
    const text = (root.textContent ?? '').replace(/\s+/g, ' ').trim();

    expect(text).toContain('Delivery');
    expect(text).toContain('Source repository');
    expect(text).toContain('Branch design/hotel-rheinland-theme');
    expect(text).toContain('Pull request #1 · Open');
    expect(text).toContain('Job workspace');
    expect(text).not.toContain('Browse workspace in Gitea');

    expect(fixture.componentInstance.sourceRepositoryUrl()).toBe(
      'https://github.com/Knaeckebrothero/KurortEngine',
    );
    expect(fixture.componentInstance.deliveryBranchUrl()).toBe(
      'https://github.com/Knaeckebrothero/KurortEngine/tree/design/hotel-rheinland-theme',
    );
    expect(fixture.componentInstance.pullRequest()?.url).toBe(PR_URL);
    expect(fixture.componentInstance.getJobWorkspaceUrl()).toMatch(/\/job-29c28492$/);
  });

  it('keeps the PR link and states when the live read is unavailable', async () => {
    const root = await render(undefined, null);
    const text = (root.textContent ?? '').replace(/\s+/g, ' ').trim();

    expect(text).toContain('Pull request #1 · Status unavailable');
    expect(fixture.componentInstance.pullRequest()?.url).toBe(PR_URL);
  });

  it('does not mislabel the connector default as the historical job delivery branch', async () => {
    const root = await render({ cloud_baseline: { status: 'captured' } });
    const text = (root.textContent ?? '').replace(/\s+/g, ' ').trim();

    expect(text).toContain('Source repository');
    expect(text).toContain('Job workspace');
    expect(text).not.toContain('Branch main');
    expect(text).not.toContain('Pull request #');
  });
});
