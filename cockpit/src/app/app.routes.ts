import {Routes} from '@angular/router';
import {SettingsComponent} from './views/settings/settings.component';
import {ApiKeysPageComponent} from './views/settings/api-keys/api-keys-page.component';
import {ShellPageComponent} from './views/shell/shell.component';
import {JobsPageComponent} from './views/jobs/jobs-page.component';
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
import {AutomationsPageComponent} from './views/automations/automations-page.component';
import {AdminLlmComponent} from './views/admin/llm/admin-llm.component';
import {AdminUsersComponent} from './views/admin/users/admin-users.component';
import {authGuard} from './core/guards/auth.guard';
import {adminGuard} from './core/guards/admin.guard';
import {projectAccessGuard} from './core/guards/project-access.guard';

export const routes: Routes = [
  { path: '', component: ShellPageComponent, canActivate: [authGuard] },
    {path: 'sessions', component: SessionsPageComponent, canActivate: [authGuard]},
    {path: 'sessions/new', component: SessionCreateComponent, canActivate: [authGuard]},
    {path: 'sessions/:threadId', component: ChatPageComponent, canActivate: [authGuard]},
    {path: 'chat', redirectTo: 'sessions'},
  { path: 'jobs', component: JobsPageComponent, canActivate: [authGuard] },
  { path: 'create', component: CreatePageComponent, canActivate: [authGuard] },
  { path: 'inbox', component: InboxPageComponent, canActivate: [authGuard] },
  { path: 'projects', component: ProjectListPageComponent, canActivate: [authGuard] },
  { path: 'projects/:id', component: ProjectDetailPageComponent, canActivate: [authGuard, projectAccessGuard] },
  { path: 'datasources', component: DatasourcesPageComponent, canActivate: [authGuard] },
  { path: 'automations', component: AutomationsPageComponent, canActivate: [authGuard] },
  { path: 'settings', component: SettingsComponent, canActivate: [authGuard] },
  { path: 'settings/api-keys', component: ApiKeysPageComponent, canActivate: [authGuard] },
  { path: 'admin/llm', component: AdminLlmComponent, canActivate: [authGuard, adminGuard] },
  { path: 'admin/providers', redirectTo: 'admin/llm' },
  { path: 'admin/models', redirectTo: 'admin/llm' },
  { path: 'admin/users', component: AdminUsersComponent, canActivate: [authGuard, adminGuard] },
  { path: 'debug', component: DebugPageComponent, canActivate: [authGuard] },

  // Redirects for old bookmarks
  { path: 'sudo', redirectTo: 'inbox' },
  { path: 'review', redirectTo: 'inbox' },

  // Catch old email links: /jobs/{jobId}/messages/{threadId}
  // redirectTo can't transform path params to query params, so use a redirect component
  { path: 'jobs/:jobId/messages/:threadId', component: MessageRedirectComponent },

  { path: '**', redirectTo: '' },
];
