import {Routes} from '@angular/router';
import {SettingsComponent} from './views/settings/settings.component';
import {ApiKeysPageComponent} from './views/settings/api-keys/api-keys-page.component';
import {JobsPageComponent} from './views/jobs/jobs-page.component';
import {JobReviewPageComponent} from './views/job-review/job-review-page.component';
import {CreatePageComponent} from './views/create/create-page.component';
import {ProjectListPageComponent} from './views/projects/project-list.component';
import {ProjectDetailPageComponent} from './views/project-detail/project-detail.component';
import {InboxPageComponent} from './views/inbox/inbox-page.component';
import {MessageRedirectComponent} from './core/routing/message-redirect/message-redirect.component';
import {ChatPageComponent} from './views/chat/chat-page.component';
import {CanvasPopoutPageComponent} from './views/canvas/canvas-popout-page.component';
import {SessionsPageComponent} from './views/sessions/sessions-page.component';
import {SessionCreateComponent} from './views/session-create/session-create.component';
import {DatasourcesPageComponent} from './views/datasources/datasources-page.component';
import {ExpertsPageComponent} from './views/experts/experts-page.component';
import {ExpertEditorComponent} from './views/experts/expert-editor.component';
import {SkillsPageComponent} from './views/skills/skills-page.component';
import {SkillEditorComponent} from './views/skills/skill-editor.component';
import {AutomationsPageComponent} from './views/automations/automations-page.component';
import {authGuard} from './core/guards/auth.guard';
import {adminGuard} from './core/guards/admin.guard';
import {projectAccessGuard} from './core/guards/project-access.guard';

export const routes: Routes = [
  // Instant landing (docs/features/instant_landing_session.md): the root is a
  // fresh draft chat — open composer, nothing created until the first send.
  // (Replaces the sessions-list redirect left by the builder removal, see
  // docs/features/builder_to_sessions_consolidation.md.)
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
  { path: 'datasources', component: DatasourcesPageComponent, canActivate: [authGuard] },
  { path: 'experts', component: ExpertsPageComponent, canActivate: [authGuard] },
  { path: 'experts/new', component: ExpertEditorComponent, canActivate: [authGuard] },
  { path: 'experts/:id/edit', component: ExpertEditorComponent, canActivate: [authGuard] },
  { path: 'skills', component: SkillsPageComponent, canActivate: [authGuard] },
  { path: 'skills/new', component: SkillEditorComponent, canActivate: [authGuard] },
  { path: 'skills/:id/edit', component: SkillEditorComponent, canActivate: [authGuard] },
  { path: 'automations', component: AutomationsPageComponent, canActivate: [authGuard] },
  { path: 'settings', component: SettingsComponent, canActivate: [authGuard] },
  { path: 'settings/api-keys', component: ApiKeysPageComponent, canActivate: [authGuard] },
  // Admin and debug load on demand. They are large (the config, usage and
  // grants screens alone are most of a megabyte of source, and debug pulls
  // the graph timeline), and no ordinary session ever opens them — keeping
  // them in the initial bundle taxed every page load to serve a handful of
  // admin visits, and pushed the build past its initial-bundle budget.
  {
    path: 'admin/llm',
    loadComponent: () =>
      import('./views/admin/llm/admin-llm.component').then(m => m.AdminLlmComponent),
    canActivate: [authGuard, adminGuard],
  },
  { path: 'admin/providers', redirectTo: 'admin/llm' },
  { path: 'admin/models', redirectTo: 'admin/llm' },
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
    path: 'debug',
    loadComponent: () =>
      import('./debug/pages/debug.component').then(m => m.DebugPageComponent),
    canActivate: [authGuard],
  },

  // Redirects for old bookmarks. Jobs absorbed the standalone Create + Review
  // surfaces, so their old top-level paths now redirect into /jobs/*.
  { path: 'sudo', redirectTo: 'inbox' },
  { path: 'create', redirectTo: 'jobs/new' },
  { path: 'review', redirectTo: 'jobs/review' },

  // Catch old email links: /jobs/{jobId}/messages/{threadId}
  // redirectTo can't transform path params to query params, so use a redirect component
  { path: 'jobs/:jobId/messages/:threadId', component: MessageRedirectComponent },

  { path: '**', redirectTo: '' },
];
