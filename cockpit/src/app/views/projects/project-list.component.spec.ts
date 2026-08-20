import {
  Component,
  EventEmitter,
  Input,
  Output,
  inject,
  signal,
  ɵresolveComponentResources,
} from '@angular/core';
import {HttpErrorResponse} from '@angular/common/http';
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
import {Datasource, Project, ProjectCreateRequest, ProjectStatus} from '../../core/models/api.model';
import {AppInlineEditableTextComponent} from '../../ui/inline-editable-text';
import {AppTabBarComponent, AppTabComponent} from '../../ui/tab-bar';
import {ProjectListPageComponent} from './project-list.component';

// The real catalogue, so the specs also prove the `projects.externalKb.*`
// keys exist (a missing key renders as the key itself, which is asserted
// against below).
import en from '../../../assets/i18n/en.json';

/**
 * Test doubles for `ui/tab-bar`, swapped in via `overrideComponent`.
 *
 * NOT a preference — a constraint of this test environment. Specs here run the
 * JIT compiler over the decorator metadata, and JIT cannot see initializer-based
 * inputs (`input()`, `model()`), so property-binding one is NG0303 "isn't a
 * known property" and a required one then throws NG0950 on first change
 * detection. That applies to every signal-input component in `ui/`, not just
 * these two; the real bar is exercised by AOT in the browser. The doubles use
 * decorator inputs, which JIT does see, and keep the contract this page depends
 * on: the selected value in, a click on a tab out.
 */
@Component({
  selector: 'app-tab-bar',
  standalone: true,
  template: `<ng-content></ng-content>`,
})
class TabBarStub {
  @Input() value: string | null = null;
  @Output() valueChange = new EventEmitter<string>();
}

@Component({
  selector: 'app-tab',
  standalone: true,
  template: `<ng-content></ng-content>`,
  host: {
    '(click)': 'select()',
    '[attr.data-value]': 'value',
    '[attr.data-active]': 'bar?.value === value || null',
  },
})
class TabStub {
  @Input() value = '';
  protected readonly bar = inject(TabBarStub, {optional: true});
  select(): void {
    this.bar?.valueChange.emit(this.value);
  }
}

/** Same JIT constraint as the tab doubles: `app-inline-editable-text` takes a
 *  required signal input, so a rendered project card cannot mount the real one
 *  here. Renders the name, which is all these specs read off a card. */
@Component({
  selector: 'app-inline-editable-text',
  standalone: true,
  template: `{{ value }}`,
})
class InlineEditableTextStub {
  @Input() value = '';
  @Input() ariaLabel = '';
  @Output() save = new EventEmitter<string>();
}

function project(overrides: Partial<Project> = {}): Project {
  return {
    id: 'proj-1',
    name: 'Better Resavio',
    description: null,
    goal: null,
    status: 'active',
    is_default: false,
    created_at: '',
    updated_at: '',
    ...overrides,
  } as Project;
}

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

/** `getProjects` answers per requested status, which is the whole point of the
 *  lifecycle filter: the tab is the query, not a client-side `.filter()`. */
function stubApi(
  createResult: unknown = {id: 'proj-1', name: 'Vault'},
  connectors: Datasource[] = [connector()],
  lists: {active?: Project[]; archived?: Project[]} = {},
) {
  return {
    getProjects: vi
      .fn()
      .mockImplementation((_userId?: string, status?: ProjectStatus[]) =>
        of(status?.[0] === 'archived' ? lists.archived ?? [] : lists.active ?? []),
      ),
    getDatasources: vi.fn().mockReturnValue(of(connectors)),
    createProject: vi.fn().mockReturnValue(of(createResult)),
    updateProject: vi.fn().mockReturnValue(of(null)),
    updateProjectFields: vi.fn().mockReturnValue(of({status: 'updated'})),
    setProjectStatus: vi.fn().mockReturnValue(of({archived: false})),
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
  TestBed.overrideComponent(ProjectListPageComponent, {
    remove: {imports: [AppTabBarComponent, AppTabComponent, AppInlineEditableTextComponent]},
    add: {imports: [TabBarStub, TabStub, InlineEditableTextStub]},
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

describe('ProjectListPageComponent — archived lifecycle', () => {
  beforeAll(async () => {
    await ɵresolveComponentResources(() => Promise.resolve(''));
  });

  afterEach(() => TestBed.resetTestingModule());

  const ARCHIVED = project({
    id: 'proj-archived',
    name: 'Better Resavio (pre-split archive)',
    status: 'archived',
  });

  function statusesRequested(api: ReturnType<typeof stubApi>): string[][] {
    return api.getProjects.mock.calls.map((call) => (call[1] as ProjectStatus[]) ?? []);
  }

  function tabs(fixture: ComponentFixture<ProjectListPageComponent>): HTMLElement[] {
    return Array.from(fixture.nativeElement.querySelectorAll('app-tab'));
  }

  function clickTab(
    fixture: ComponentFixture<ProjectListPageComponent>,
    value: ProjectStatus,
  ): void {
    const tab = tabs(fixture).find((el) => el.getAttribute('data-value') === value)!;
    tab.click();
    fixture.detectChanges();
  }

  function cardNames(fixture: ComponentFixture<ProjectListPageComponent>): string[] {
    return Array.from(
      fixture.nativeElement.querySelectorAll('.project-card .card-name'),
    ).map((el) => (el as HTMLElement).textContent?.trim() ?? '');
  }

  function unarchiveButtons(
    fixture: ComponentFixture<ProjectListPageComponent>,
  ): HTMLButtonElement[] {
    return Array.from(fixture.nativeElement.querySelectorAll('.project-card .card-action'));
  }

  it('asks the server for active projects only, and for the archived count beside it', async () => {
    // The filter is server-side. A client-side `.filter()` would still pay for
    // the rows in Postgres and leave every other consumer unprotected.
    const api = stubApi(undefined, [], {active: [project()], archived: [ARCHIVED]});
    const fixture = await mount(api);

    expect(statusesRequested(api)).toEqual([['active'], ['archived']]);
    expect(cardNames(fixture)).toEqual(['Better Resavio']);
  });

  it('re-queries with ?status=archived when the Archived tab is chosen', async () => {
    const api = stubApi(undefined, [], {active: [project()], archived: [ARCHIVED]});
    const fixture = await mount(api);
    api.getProjects.mockClear();

    clickTab(fixture, 'archived');
    await fixture.whenStable();
    fixture.detectChanges();

    expect(statusesRequested(api)).toContainEqual(['archived']);
    expect(cardNames(fixture)).toEqual(['Better Resavio (pre-split archive)']);
    // Hiding is never silent: the tab says how many are over there, and the
    // view says where they went.
    expect(fixture.nativeElement.querySelector('.list-notice')?.textContent?.trim()).toBe(
      en.projects.archivedNotice,
    );
  });

  it('counts the archived tab while the active one is showing', async () => {
    const api = stubApi(undefined, [], {
      active: [project(), project({id: 'p-2', name: 'Second'})],
      archived: [ARCHIVED],
    });
    const fixture = await mount(api);

    const labels = tabs(fixture).map((el) => el.textContent?.trim());
    expect(labels).toEqual(['Active (2)', 'Archived (1)']);
    // Rendered from the catalogue, not hardcoded English.
    expect(labels[1]).toBe(en.projects.filter.archived.replace('{{count}}', '1'));
  });

  it('offers Unarchive on archived cards and on those only', async () => {
    const api = stubApi(undefined, [], {active: [project()], archived: [ARCHIVED]});
    const fixture = await mount(api);
    expect(unarchiveButtons(fixture)).toHaveLength(0);

    clickTab(fixture, 'archived');
    await fixture.whenStable();
    fixture.detectChanges();

    const [button] = unarchiveButtons(fixture);
    expect(button.textContent?.trim()).toBe(en.projects.action.unarchive);
    button.click();

    expect(api.setProjectStatus).toHaveBeenCalledWith('proj-archived', 'active');
  });

  it('does not open the project when the card action is clicked', async () => {
    const router = {navigate: vi.fn()};
    const api = stubApi(undefined, [], {archived: [ARCHIVED]});
    const fixture = await mount(api, router);
    clickTab(fixture, 'archived');
    await fixture.whenStable();
    fixture.detectChanges();

    unarchiveButtons(fixture)[0].click();

    expect(router.navigate).not.toHaveBeenCalled();
  });

  it("shows the server's own sentence when an unarchive is refused", async () => {
    // The 409 body is a plain-string `detail`, which is what house style sends
    // and what the error service renders verbatim. Before this change the PATCH
    // went through `updateProject`, which mapped every failure to `null`, under
    // a subscribe with no error callback — the refusal reached nobody.
    const api = stubApi(undefined, [], {archived: [ARCHIVED]});
    const detail = 'This project is archived. Unarchive it before creating new work.';
    api.setProjectStatus = vi
      .fn()
      .mockReturnValue(throwError(() => new HttpErrorResponse({status: 409, error: {detail}})));
    const fixture = await mount(api);
    clickTab(fixture, 'archived');
    await fixture.whenStable();
    fixture.detectChanges();

    unarchiveButtons(fixture)[0].click();
    fixture.detectChanges();

    const error = fixture.nativeElement.querySelector('.list-error') as HTMLElement;
    expect(error.textContent?.trim()).toBe(detail);
    // The row is still there to try again on.
    expect(cardNames(fixture)).toHaveLength(1);
  });

  it('reports a failed load instead of rendering "no projects yet"', async () => {
    // `getProjects` used to funnel every error into `of([])`, which renders as
    // an empty account — the one reading a user must never be given wrongly.
    const api = stubApi(undefined, [], {active: [project()]});
    const fixture = await mount(api);
    api.getProjects = vi
      .fn()
      .mockReturnValue(throwError(() => new HttpErrorResponse({status: 500, error: {}})));

    (fixture.nativeElement.querySelector('.header-actions .btn-ghost') as HTMLButtonElement).click();
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('.empty-state')).toBeNull();
    expect((fixture.nativeElement.querySelector('.list-error') as HTMLElement).textContent?.trim())
      .toBe(en.errors.http['5xx']);
  });

  function renameControls(
    fixture: ComponentFixture<ProjectListPageComponent>,
  ): HTMLElement[] {
    return Array.from(
      fixture.nativeElement.querySelectorAll('.project-card .card-name app-inline-editable-text'),
    );
  }

  it('offers no rename on an archived card, and still shows the name', async () => {
    // An archived project takes a status-only PATCH; a rename is refused whole
    // with a 409. Offering the control means the title can be typed into and
    // then snap back, which is the failure this removes rather than reports.
    const api = stubApi(undefined, [], {active: [project()], archived: [ARCHIVED]});
    const fixture = await mount(api);
    expect(renameControls(fixture)).toHaveLength(1);

    clickTab(fixture, 'archived');
    await fixture.whenStable();
    fixture.detectChanges();

    expect(renameControls(fixture)).toHaveLength(0);
    // Read-only, not hidden.
    expect(cardNames(fixture)).toEqual(['Better Resavio (pre-split archive)']);
  });

  it('sends no rename for an archived project even if one is emitted', async () => {
    const api = stubApi(undefined, [], {archived: [ARCHIVED]});
    const fixture = await mount(api);
    clickTab(fixture, 'archived');
    await fixture.whenStable();
    fixture.detectChanges();

    fixture.componentInstance.onRenameProject(ARCHIVED, 'Something else');

    expect(api.updateProjectFields).not.toHaveBeenCalled();
    expect(api.updateProject).not.toHaveBeenCalled();
    expect(cardNames(fixture)).toEqual(['Better Resavio (pre-split archive)']);
  });

  it("says why a rename snapped back when the project was archived elsewhere", async () => {
    // Prevention loses this race: the card was rendered as active. The rename
    // used to go through `updateProject`, whose `null` on error meant the name
    // reverted and nothing said why.
    const api = stubApi(undefined, [], {active: [project()]});
    const detail =
      'This project is archived and is read-only apart from its status. ' +
      'Unarchive it before editing anything else.';
    api.updateProjectFields = vi
      .fn()
      .mockReturnValue(throwError(() => new HttpErrorResponse({status: 409, error: {detail}})));
    const fixture = await mount(api);

    fixture.componentInstance.onRenameProject(project(), 'Renamed');
    fixture.detectChanges();

    expect(cardNames(fixture)).toEqual(['Better Resavio']);
    expect(
      (fixture.nativeElement.querySelector('.list-error') as HTMLElement).textContent?.trim(),
    ).toBe(detail);
  });

  it('names the empty archived view for what it is', async () => {
    const fixture = await mount(stubApi(undefined, [], {active: [project()]}));
    clickTab(fixture, 'archived');
    await fixture.whenStable();
    fixture.detectChanges();

    const empty = fixture.nativeElement.querySelector('.empty-state') as HTMLElement;
    expect(empty.textContent).toContain(en.projects.emptyArchived);
    expect(empty.textContent).not.toContain('projects.empty');
  });
});
