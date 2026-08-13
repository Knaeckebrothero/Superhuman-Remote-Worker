import {signal, ɵresolveComponentResources} from '@angular/core';
import {TestBed} from '@angular/core/testing';
import {ComponentFixture} from '@angular/core/testing';
import {Router} from '@angular/router';
import {TranslocoTestingModule} from '@jsverse/transloco';
import {of} from 'rxjs';
import {afterEach, beforeAll, beforeEach, describe, expect, it, vi} from 'vitest';
import {ApiService} from '../../core/services/api.service';
import {SessionService} from '../../core/services/session.service';
import {SidebarService} from '../../core/services/sidebar.service';
import {UserService} from '../../core/services/user.service';
import {ViewportService} from '../../core/services/viewport.service';
import {ProjectCreateRequest} from '../../core/models/api.model';
import {ProjectListPageComponent} from './project-list.component';

// The real catalogue, so the specs also prove the `projects.externalKb.*`
// keys exist (a missing key renders as the key itself, which is asserted
// against below).
import en from '../../../assets/i18n/en.json';

/** Order of `input.form-input` inside `.create-form` once the external-KB
 *  section is revealed. The checkbox is not a `.form-input`. */
const NAME = 0;
const REPO_URL = 3;
const BRANCH = 4;
const TOKEN = 5;

function stubApi(createResult: unknown = {id: 'proj-1', name: 'Vault'}) {
  return {
    getProjects: vi.fn().mockReturnValue(of([])),
    createProject: vi.fn().mockReturnValue(of(createResult)),
    updateProject: vi.fn().mockReturnValue(of(null)),
  };
}

async function mount(api: ReturnType<typeof stubApi>) {
  TestBed.configureTestingModule({
    imports: [
      ProjectListPageComponent,
      TranslocoTestingModule.forRoot({
        langs: {en},
        translocoConfig: {availableLangs: ['en'], defaultLang: 'en'},
      }),
    ],
    providers: [
      {provide: ApiService, useValue: api},
      {provide: UserService, useValue: {currentUserId: signal('user-1')}},
      {provide: SessionService, useValue: {forceRefresh: vi.fn().mockResolvedValue(undefined)}},
      {provide: Router, useValue: {navigate: vi.fn()}},
      // Stubbed so the specs do not depend on jsdom's matchMedia.
      {provide: ViewportService, useValue: {isMobile: signal(false)}},
      {provide: SidebarService, useValue: {collapsed: signal(false), expand: vi.fn()}},
    ],
  });
  await TestBed.compileComponents();
  const fixture = TestBed.createComponent(ProjectListPageComponent);
  fixture.detectChanges();
  await fixture.whenStable();
  fixture.detectChanges();
  return fixture;
}

function openCreateForm(fixture: ComponentFixture<ProjectListPageComponent>): void {
  const newProject = fixture.nativeElement.querySelector(
    '.header-actions .btn-primary',
  ) as HTMLButtonElement;
  newProject.click();
  fixture.detectChanges();
}

function inputs(fixture: ComponentFixture<ProjectListPageComponent>): HTMLInputElement[] {
  return Array.from(fixture.nativeElement.querySelectorAll('.create-form input.form-input'));
}

function type(
  fixture: ComponentFixture<ProjectListPageComponent>,
  index: number,
  value: string,
): void {
  const field = inputs(fixture)[index];
  field.value = value;
  field.dispatchEvent(new Event('input'));
  fixture.detectChanges();
}

function toggleExternalKb(fixture: ComponentFixture<ProjectListPageComponent>): void {
  const checkbox = fixture.nativeElement.querySelector(
    '.create-form input[type="checkbox"]',
  ) as HTMLInputElement;
  checkbox.click();
  fixture.detectChanges();
}

function submitButton(
  fixture: ComponentFixture<ProjectListPageComponent>,
): HTMLButtonElement {
  return fixture.nativeElement.querySelector('.form-actions .btn-primary') as HTMLButtonElement;
}

function createdBody(api: ReturnType<typeof stubApi>): ProjectCreateRequest {
  expect(api.createProject).toHaveBeenCalledTimes(1);
  return api.createProject.mock.calls[0][0] as ProjectCreateRequest;
}

describe('ProjectListPageComponent — external knowledge base', () => {
  let api: ReturnType<typeof stubApi>;

  // The nested app-icon inside app-sidebar-toggle carries an external
  // styleUrl, which JIT compilation in jsdom refuses to resolve on its own.
  beforeAll(async () => {
    await ɵresolveComponentResources(() => Promise.resolve(''));
  });

  beforeEach(() => {
    api = stubApi();
  });

  afterEach(() => TestBed.resetTestingModule());

  it('is collapsed by default and omits external_kb entirely when off', async () => {
    const fixture = await mount(api);
    openCreateForm(fixture);

    // Collapsed: only name/description/goal are rendered.
    expect(inputs(fixture)).toHaveLength(3);
    expect(fixture.nativeElement.querySelector('input[type="password"]')).toBeNull();

    type(fixture, NAME, 'Plain project');
    submitButton(fixture).click();

    const body = createdBody(api);
    expect(body.name).toBe('Plain project');
    // Not merely undefined — the key must be absent from the payload.
    expect('external_kb' in body).toBe(false);
    expect(JSON.stringify(body)).not.toContain('external_kb');
  });

  it('sends repo_url, branch and token when the toggle is on', async () => {
    const fixture = await mount(api);
    openCreateForm(fixture);
    toggleExternalKb(fixture);

    type(fixture, NAME, 'Vault project');
    type(fixture, REPO_URL, '  https://github.com/acme/vault  ');
    type(fixture, BRANCH, 'notes');
    type(fixture, TOKEN, 'github_pat_secret');
    submitButton(fixture).click();

    expect(createdBody(api).external_kb).toEqual({
      repo_url: 'https://github.com/acme/vault',
      token: 'github_pat_secret',
      branch: 'notes',
    });
  });

  it('omits branch when left blank so the backend default applies', async () => {
    const fixture = await mount(api);
    openCreateForm(fixture);
    toggleExternalKb(fixture);

    type(fixture, NAME, 'Vault project');
    type(fixture, REPO_URL, 'https://github.com/acme/vault');
    type(fixture, TOKEN, 'github_pat_secret');
    submitButton(fixture).click();

    const external = createdBody(api).external_kb!;
    expect(external).toEqual({repo_url: 'https://github.com/acme/vault', token: 'github_pat_secret'});
    expect('branch' in external).toBe(false);
  });

  it('keeps submit disabled until repo URL and token are both filled in', async () => {
    const fixture = await mount(api);
    openCreateForm(fixture);

    type(fixture, NAME, 'Vault project');
    expect(submitButton(fixture).disabled).toBe(false);

    toggleExternalKb(fixture);
    expect(submitButton(fixture).disabled).toBe(true);

    type(fixture, REPO_URL, 'https://github.com/acme/vault');
    expect(submitButton(fixture).disabled).toBe(true);

    type(fixture, TOKEN, 'github_pat_secret');
    expect(submitButton(fixture).disabled).toBe(false);

    // Whitespace-only is not a value.
    type(fixture, REPO_URL, '   ');
    expect(submitButton(fixture).disabled).toBe(true);
  });

  it('masks the token, keeps it out of storage, and clears it after a successful create', async () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem');
    const fixture = await mount(api);
    openCreateForm(fixture);
    toggleExternalKb(fixture);

    const token = fixture.nativeElement.querySelector(
      '.create-form input[type="password"]',
    ) as HTMLInputElement;
    expect(token).not.toBeNull();
    expect(token.getAttribute('autocomplete')).toBe('off');

    type(fixture, NAME, 'Vault project');
    type(fixture, REPO_URL, 'https://github.com/acme/vault');
    type(fixture, TOKEN, 'github_pat_secret');
    submitButton(fixture).click();
    fixture.detectChanges();

    const component = fixture.componentInstance;
    expect(component.formKbToken()).toBe('');
    expect(component.formKbRepoUrl()).toBe('');
    expect(component.useExternalKb()).toBe(false);
    expect(component.showCreateForm()).toBe(false);
    expect(setItem.mock.calls.some(([, value]) => String(value).includes('github_pat_secret'))).toBe(
      false,
    );
    setItem.mockRestore();
  });

  it('drops the token when the section is collapsed again', async () => {
    const fixture = await mount(api);
    openCreateForm(fixture);
    toggleExternalKb(fixture);
    type(fixture, TOKEN, 'github_pat_secret');
    expect(fixture.componentInstance.formKbToken()).toBe('github_pat_secret');

    toggleExternalKb(fixture);
    expect(fixture.componentInstance.formKbToken()).toBe('');
    expect(fixture.nativeElement.querySelector('input[type="password"]')).toBeNull();
  });

  it('shows a translated inline error when the create fails', async () => {
    const failing = stubApi(null);
    const fixture = await mount(failing);
    openCreateForm(fixture);
    type(fixture, NAME, 'Doomed project');

    expect(fixture.nativeElement.querySelector('.form-error')).toBeNull();
    submitButton(fixture).click();
    fixture.detectChanges();

    const error = fixture.nativeElement.querySelector('.form-error') as HTMLElement;
    expect(error).not.toBeNull();
    expect(error.textContent?.trim()).toBe(en.projects.createFailed);
    // The form stays open with its input intact so the user can retry.
    expect(fixture.componentInstance.showCreateForm()).toBe(true);
    expect(fixture.componentInstance.isCreating()).toBe(false);
  });

  it('renders translated labels rather than raw i18n keys', async () => {
    const fixture = await mount(api);
    openCreateForm(fixture);
    toggleExternalKb(fixture);

    const form = fixture.nativeElement.querySelector('.create-form') as HTMLElement;
    expect(form.textContent).toContain(en.projects.externalKb.toggle);
    expect(form.textContent).toContain(en.projects.externalKb.hint);
    expect(form.textContent).not.toContain('projects.externalKb');
    expect(inputs(fixture)[REPO_URL].placeholder).toBe(en.projects.externalKb.repoUrlPlaceholder);
    expect(inputs(fixture)[BRANCH].placeholder).toBe(en.projects.externalKb.branchPlaceholder);
    expect(inputs(fixture)[TOKEN].placeholder).toBe(en.projects.externalKb.tokenPlaceholder);
  });
});
