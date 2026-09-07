import {Routes} from '@angular/router';
import {SettingsComponent} from './views/settings/settings.component';
import {ApiKeysPageComponent} from './views/settings/api-keys/api-keys-page.component';
import {JobsPageComponent} from './views/jobs/jobs-page.component';
import {JobReviewPageComponent} from './views/job-review/job-review-page.component';
import {CreatePageComponent} from './views/create/create-page.component';
import {ProjectListPageComponent} from './views/projects/project-list.component';
import {ProjectDetailPageComponent} from './views/project-detail/project-detail.component';
import {ConferenceLauncherComponent} from './views/project-detail/conference-launcher.component';
import {InboxPageComponent} from './views/inbox/inbox-page.component';
import {MessageRedirectComponent} from './core/routing/message-redirect/message-redirect.component';
import {ChatPageComponent} from './views/chat/chat-page.component';
import {CanvasPopoutPageComponent} from './views/canvas/canvas-popout-page.component';
import {SessionsPageComponent} from './views/sessions/sessions-page.component';
import {SessionCreateComponent} from './views/session-create/session-create.component';
import {ExpertsPageComponent} from './views/experts/experts-page.component';
import {ExpertEditorComponent} from './views/experts/expert-editor.component';
import {SkillsPageComponent} from './views/skills/skills-page.component';
import {SkillEditorComponent} from './views/skills/skill-editor.component';
import {authGuard} from './core/guards/auth.guard';
import {adminGuard} from './core/guards/admin.guard';
import {projectAccessGuard} from './core/guards/project-access.guard';

export const routes: Routes = [
  // Instant landing (knowledge-base/knowledge/features/instant_landing_session.md): the root is a
  // fresh draft chat — open composer, nothing created until the first send.
  // (Replaces the sessions-list redirect left by the builder removal, see
  // knowledge-base/knowledge/features/builder_to_sessions_consolidation.md.)
  { path: '', component: ChatPageComponent, canActivate: [authGuard], data: { draft: true } },
    {path: 'sessions', component: SessionsPageComponent, canActivate: [authGuard]},
    {path: 'sessions/new', component: SessionCreateComponent, canActivate: [authGuard]},
    {
      path: 'sessions/:threadId/canvas',
      component: CanvasPopoutPageComponent,
      canActivate: [authGuard],
      data: {canvasPopout: true},
    },
    {path: 'sessions/:threadId', component: ChatPageComponent, canActivate: [authGuard]},
    {path: 'chat', redirectTo: 'sessions'},
  { path: 'jobs', component: JobsPageComponent, canActivate: [authGuard] },
  { path: 'jobs/new', component: CreatePageComponent, canActivate: [authGuard] },
  { path: 'jobs/review', component: JobReviewPageComponent, canActivate: [authGuard] },
  { path: 'inbox', component: InboxPageComponent, canActivate: [authGuard] },
  { path: 'projects', component: ProjectListPageComponent, canActivate: [authGuard] },
  { path: 'projects/:id', component: ProjectDetailPageComponent, canActivate: [authGuard, projectAccessGuard] },
  {
    path: 'projects/:id/officer/conference',
    component: ConferenceLauncherComponent,
    canActivate: [authGuard, projectAccessGuard],
  },
  {
    path: 'datasources',
    loadComponent: () =>
      import('./views/datasources/datasources-page.component').then(m => m.DatasourcesPageComponent),
    canActivate: [authGuard],
  },
  {
    path: 'contacts',
    loadComponent: () =>
      import('./views/contacts/contacts-page.component').then(m => m.ContactsPageComponent),
    canActivate: [authGuard],
  },
  { path: 'experts', component: ExpertsPageComponent, canActivate: [authGuard] },
  { path: 'experts/new', component: ExpertEditorComponent, canActivate: [authGuard] },
  { path: 'experts/:id/edit', component: ExpertEditorComponent, canActivate: [authGuard] },
  { path: 'skills', component: SkillsPageComponent, canActivate: [authGuard] },
  { path: 'skills/new', component: SkillEditorComponent, canActivate: [authGuard] },
  { path: 'skills/:id/edit', component: SkillEditorComponent, canActivate: [authGuard] },
  // Automations loads on demand: the schedule editor is the only screen that
  // needs cronstrue + cron-parser, and both are CommonJS, so keeping the route
  // eager taxed every page load with a cron library it would never call.
  {
    path: 'automations',
    loadComponent: () =>
      import('./views/automations/automations-page.component').then(m => m.AutomationsPageComponent),
    canActivate: [authGuard],
  },
  { path: 'settings', component: SettingsComponent, canActivate: [authGuard] },
  { path: 'settings/api-keys', component: ApiKeysPageComponent, canActivate: [authGuard] },
  // Lazy, unlike the PAT route above: the bundle has ~30 KB of headroom
  // against the hard-fail initial-bundle budget, and this page (plus the
  // ssh-keygen instructions it renders) is not something every session
  // visits — see the `automations` route's rationale.
  {
    path: 'settings/ssh-keys',
    loadComponent: () =>
      import('./views/settings/ssh-keys/ssh-keys-page.component').then(
        (m) => m.SshKeysPageComponent,
      ),
    canActivate: [authGuard],
  },
  // Admin and the workbench load on demand. They are large (the config, usage
  // and grants screens alone are most of a megabyte of source, and the
  // workbench pulls the graph timeline), and no ordinary session ever opens
  // them — keeping them in the initial bundle taxed every page load to serve a
  // handful of admin visits, and pushed the build past its initial-bundle
  // budget.
  {
    path: 'admin/models',
    loadComponent: () =>
      import('./views/admin/models/admin-models.component').then(m => m.AdminModelsComponent),
    canActivate: [authGuard, adminGuard],
  },
  // The page was 'admin/llm' until the catalog grew past chat models — it now
  // holds TTS, speech-to-text, vision and embedding entries too, so the name
  // described a third of its contents. Both former paths still resolve;
  // 'admin/llm' in particular is in the wild via the readiness-gate banners.
  { path: 'admin/providers', redirectTo: 'admin/models' },
  { path: 'admin/llm', redirectTo: 'admin/models' },
  {
    path: 'admin/users',
    loadComponent: () =>
      import('./views/admin/users/admin-users.component').then(m => m.AdminUsersComponent),
    canActivate: [authGuard, adminGuard],
  },
  {
    path: 'admin/config',
    loadComponent: () =>
      import('./views/admin/config/admin-config.component').then(m => m.AdminConfigComponent),
    canActivate: [authGuard, adminGuard],
  },
  {
    path: 'admin/grants',
    loadComponent: () =>
      import('./views/admin/grants/admin-grants.component').then(m => m.AdminGrantsComponent),
    canActivate: [authGuard, adminGuard],
  },
  {
    path: 'admin/usage',
    loadComponent: () =>
      import('./views/admin/usage/admin-usage.component').then(m => m.AdminUsageComponent),
    canActivate: [authGuard, adminGuard],
  },
  {
    path: 'workbench',
    loadComponent: () =>
      import('./workbench/pages/workbench.component').then(m => m.WorkbenchPageComponent),
    canActivate: [authGuard],
  },

  // Redirects for old bookmarks. Jobs absorbed the standalone Create + Review
  // surfaces, so their old top-level paths now redirect into /jobs/*. /debug
  // was renamed to /workbench — the surface is a customizable panel workspace,
  // not a troubleshooting console.
  { path: 'debug', redirectTo: 'workbench' },
  { path: 'sudo', redirectTo: 'inbox' },
  { path: 'create', redirectTo: 'jobs/new' },
  { path: 'review', redirectTo: 'jobs/review' },

  // Catch old email links: /jobs/{jobId}/messages/{threadId}
  // redirectTo can't transform path params to query params, so use a redirect component
  { path: 'jobs/:jobId/messages/:threadId', component: MessageRedirectComponent },

  { path: '**', redirectTo: '' },
];
