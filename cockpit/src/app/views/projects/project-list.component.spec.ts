import {signal, ɵresolveComponentResources} from '@angular/core';
import {TestBed} from '@angular/core/testing';
import {ComponentFixture} from '@angular/core/testing';
import {Router} from '@angular/router';
import {TranslocoTestingModule} from '@jsverse/transloco';
import {of, throwError} from 'rxjs';
import {afterEach, beforeAll, beforeEach, describe, expect, it, vi} from 'vitest';
import {ApiService} from '../../core/services/api.service';
import {SessionService} from '../../core/services/session.service';
import {SidebarService} from '../../core/services/sidebar.service';
import {UserService} from '../../core/services/user.service';
import {ViewportService} from '../../core/services/viewport.service';
import {Datasource, ProjectCreateRequest} from '../../core/models/api.model';
import {ProjectListPageComponent} from './project-list.component';

// The real catalogue, so the specs also prove the `projects.externalKb.*`
// keys exist (a missing key renders as the key itself, which is asserted
// against below).
import en from '../../../assets/i18n/en.json';

/** Order of `input.form-input` inside `.create-form`. The connector picker is
 *  a `<select>` and the toggle a checkbox, so neither shifts these. */
const NAME = 0;

function connector(overrides: Partial<Datasource> = {}): Datasource {
  return {
    id: 'ds-1',
    name: 'Design Vault',
    description: null,
    type: 'kb',
    connection_url: 'https://github.com/acme/design-vault.git',
    cli_hint: null,
    default_branch: 'main',
    config: {root_path: 'knowledge'},
    job_id: null,
    ...overrides,
  } as Datasource;
}

function stubApi(
  createResult: unknown = {id: 'proj-1', name: 'Vault'},
  connectors: Datasource[] = [connector()],
) {
  return {
    getProjects: vi.fn().mockReturnValue(of([])),
    getDatasources: vi.fn().mockReturnValue(of(connectors)),
    createProject: vi.fn().mockReturnValue(of(createResult)),
    updateProject: vi.fn().mockReturnValue(of(null)),
  };
}

async function mount(api: ReturnType<typeof stubApi>, router = {navigate: vi.fn()}) {
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
      {provide: Router, useValue: router},
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

function connectorSelect(
  fixture: ComponentFixture<ProjectListPageComponent>,
): HTMLSelectElement | null {
  return fixture.nativeElement.querySelector('.create-form select.form-input');
}

function pickConnector(
  fixture: ComponentFixture<ProjectListPageComponent>,
  id: string,
): void {
  const select = connectorSelect(fixture)!;
  select.value = id;
  select.dispatchEvent(new Event('change'));
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

    // Collapsed: only name/description/goal are rendered, and no connector
    // list is fetched for a project that will use the internal vault.
    expect(inputs(fixture)).toHaveLength(3);
    expect(connectorSelect(fixture)).toBeNull();
    expect(api.getDatasources).not.toHaveBeenCalled();

    type(fixture, NAME, 'Plain project');
    submitButton(fixture).click();

    const body = createdBody(api);
    expect(body.name).toBe('Plain project');
    // Not merely undefined — the key must be absent from the payload.
    expect('external_kb' in body).toBe(false);
    expect(JSON.stringify(body)).not.toContain('external_kb');
  });

  it('never collects a repository URL or token in the project form', async () => {
    // The credential belongs to the connector; a project only points at one.
    const fixture = await mount(api);
    openCreateForm(fixture);
    toggleExternalKb(fixture);

    expect(fixture.nativeElement.querySelector('.create-form input[type="password"]')).toBeNull();
    expect(inputs(fixture)).toHaveLength(3);
  });

  it('offers the knowledge-base connectors the user can attach', async () => {
    const fixture = await mount(api);
    openCreateForm(fixture);
    toggleExternalKb(fixture);

    expect(api.getDatasources).toHaveBeenCalledWith(undefined, 'kb');
    const options = Array.from(connectorSelect(fixture)!.querySelectorAll('option'));
    // A placeholder plus the one connector.
    expect(options.map((o) => o.textContent?.trim())).toContain('Design Vault');
    expect(options.some((o) => o.value === 'ds-1')).toBe(true);
  });

  it('leaves out connectors that already back a project', async () => {
    // Adopted connectors are managed by their own project; offering one here
    // would only ever produce a 409.
    const taken = connector({
      id: 'ds-taken',
      name: 'Someone Elses Vault',
      config: {root_path: 'knowledge', native_project_id: 'proj-9'},
    });
    const fixture = await mount(stubApi(undefined, [connector(), taken]));
    openCreateForm(fixture);
    toggleExternalKb(fixture);

    const values = Array.from(connectorSelect(fixture)!.querySelectorAll('option')).map(
      (o) => o.value,
    );
    expect(values).toContain('ds-1');
    expect(values).not.toContain('ds-taken');
  });

  it('sends the chosen connector and nothing else', async () => {
    const fixture = await mount(api);
    openCreateForm(fixture);
    toggleExternalKb(fixture);

    type(fixture, NAME, 'Vault project');
    pickConnector(fixture, 'ds-1');
    submitButton(fixture).click();

    expect(createdBody(api).external_kb).toEqual({datasource_id: 'ds-1'});
  });

  it('keeps submit disabled until a connector is chosen', async () => {
    const fixture = await mount(api);
    openCreateForm(fixture);

    type(fixture, NAME, 'Vault project');
    expect(submitButton(fixture).disabled).toBe(false);

    toggleExternalKb(fixture);
    expect(submitButton(fixture).disabled).toBe(true);

    pickConnector(fixture, 'ds-1');
    expect(submitButton(fixture).disabled).toBe(false);
  });

  it('points the user at connector creation when they have none', async () => {
    const router = {navigate: vi.fn()};
    const fixture = await mount(stubApi(undefined, []), router);
    openCreateForm(fixture);
    type(fixture, NAME, 'Vault project');
    toggleExternalKb(fixture);

    expect(connectorSelect(fixture)).toBeNull();
    const form = fixture.nativeElement.querySelector('.create-form') as HTMLElement;
    expect(form.textContent).toContain(en.projects.externalKb.noConnectors);
    // Nothing to attach yet, so the project cannot be created this way.
    expect(submitButton(fixture).disabled).toBe(true);

    (fixture.nativeElement.querySelector('.create-form .kb-connector-link') as HTMLButtonElement).click();
    expect(router.navigate).toHaveBeenCalledWith(['/datasources']);
  });

  it('forgets the chosen connector when the section is collapsed', async () => {
    const fixture = await mount(api);
    openCreateForm(fixture);
    toggleExternalKb(fixture);
    pickConnector(fixture, 'ds-1');
    expect(fixture.componentInstance.formKbDatasourceId()).toBe('ds-1');

    toggleExternalKb(fixture);
    expect(fixture.componentInstance.formKbDatasourceId()).toBe('');
    expect(connectorSelect(fixture)).toBeNull();
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

  it('shows the server’s reason when a connector is refused', async () => {
    // Every refusal on this path is actionable ("unlink it first", "set its
    // note root to knowledge"), so the generic message would waste the trip.
    const refusing = stubApi();
    refusing.createProject = vi
      .fn()
      .mockReturnValue(
        throwError(() => ({error: {detail: 'This connector is shared with other projects'}})),
      );
    const fixture = await mount(refusing);
    openCreateForm(fixture);
    type(fixture, NAME, 'Vault project');
    toggleExternalKb(fixture);
    pickConnector(fixture, 'ds-1');
    submitButton(fixture).click();
    fixture.detectChanges();

    const error = fixture.nativeElement.querySelector('.form-error') as HTMLElement;
    expect(error.textContent?.trim()).toBe('This connector is shared with other projects');
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
    expect(connectorSelect(fixture)!.querySelector('option')!.textContent?.trim()).toBe(
      en.projects.externalKb.selectPlaceholder,
    );
  });
});
