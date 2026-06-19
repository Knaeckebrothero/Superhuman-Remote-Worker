import {Routes} from '@angular/router';
import {SettingsComponent} from './views/settings/settings.component';
import {ApiKeysPageComponent} from './views/settings/api-keys/api-keys-page.component';
import {JobsPageComponent} from './views/jobs/jobs-page.component';
import {JobReviewPageComponent} from './views/job-review/job-review-page.component';
import {CreatePageComponent} from './views/create/create-page.component';
import {DebugPageComponent} from './debug/pages/debug.component';
import {ProjectListPageComponent} from './views/projects/project-list.component';
import {ProjectDetailPageComponent} from './views/project-detail/project-detail.component';
import {InboxPageComponent} from './views/inbox/inbox-page.component';
import {MessageRedirectComponent} from './core/routing/message-redirect/message-redirect.component';
import {ChatPageComponent} from './views/chat/chat-page.component';
import {SessionsPageComponent} from './views/sessions/sessions-page.component';
import {SessionCreateComponent} from './views/session-create/session-create.component';
import {DatasourcesPageComponent} from './views/datasources/datasources-page.component';
import {ExpertsPageComponent} from './views/experts/experts-page.component';
import {ExpertEditorComponent} from './views/experts/expert-editor.component';
import {SkillsPageComponent} from './views/skills/skills-page.component';
import {SkillEditorComponent} from './views/skills/skill-editor.component';
import {AutomationsPageComponent} from './views/automations/automations-page.component';
import {AdminLlmComponent} from './views/admin/llm/admin-llm.component';
import {AdminUsersComponent} from './views/admin/users/admin-users.component';
import {AdminConfigComponent} from './views/admin/config/admin-config.component';
import {AdminGrantsComponent} from './views/admin/grants/admin-grants.component';
import {authGuard} from './core/guards/auth.guard';
import {adminGuard} from './core/guards/admin.guard';
import {projectAccessGuard} from './core/guards/project-access.guard';

export const routes: Routes = [
  // Builder removed (see docs/features/builder_to_sessions_consolidation.md).
  // Sessions is the primary surface; root redirects there.
  { path: '', redirectTo: 'sessions', pathMatch: 'full' },
    {path: 'sessions', component: SessionsPageComponent, canActivate: [authGuard]},
    {path: 'sessions/new', component: SessionCreateComponent, canActivate: [authGuard]},
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
  { path: 'admin/llm', component: AdminLlmComponent, canActivate: [authGuard, adminGuard] },
  { path: 'admin/providers', redirectTo: 'admin/llm' },
  { path: 'admin/models', redirectTo: 'admin/llm' },
  { path: 'admin/users', component: AdminUsersComponent, canActivate: [authGuard, adminGuard] },
  { path: 'admin/config', component: AdminConfigComponent, canActivate: [authGuard, adminGuard] },
  { path: 'admin/grants', component: AdminGrantsComponent, canActivate: [authGuard, adminGuard] },
  { path: 'debug', component: DebugPageComponent, canActivate: [authGuard] },

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
