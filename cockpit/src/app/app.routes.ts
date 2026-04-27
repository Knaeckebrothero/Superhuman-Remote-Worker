import {Routes} from '@angular/router';
import {SettingsComponent} from './simple/pages/settings/settings.component';
import {ShellPageComponent} from './simple/pages/shell/shell.component';
import {JobsPageComponent} from './simple/pages/jobs/jobs-page.component';
import {CreatePageComponent} from './simple/pages/create/create-page.component';
import {DebugPageComponent} from './debug/pages/debug.component';
import {ProjectListPageComponent} from './shared/pages/project-list.component';
import {ProjectDetailPageComponent} from './simple/pages/project-detail/project-detail.component';
import {InboxPageComponent} from './simple/pages/inbox/inbox-page.component';
import {MessageRedirectComponent} from './shared/components/message-redirect/message-redirect.component';
import {ChatPageComponent} from './simple/pages/chat/chat-page.component';
import {SessionsPageComponent} from './simple/pages/sessions/sessions-page.component';
import {SessionCreateComponent} from './simple/pages/session-create/session-create.component';
import {DatasourcesPageComponent} from './simple/pages/datasources/datasources-page.component';
import {AdminProvidersComponent} from './simple/pages/admin-providers/admin-providers.component';
import {AdminModelsComponent} from './simple/pages/admin-models/admin-models.component';
import {AdminUsersComponent} from './simple/pages/admin-users/admin-users.component';
import {authGuard} from './core/guards/auth.guard';
import {adminGuard} from './core/guards/admin.guard';

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
  { path: 'projects/:id', component: ProjectDetailPageComponent, canActivate: [authGuard] },
  { path: 'datasources', component: DatasourcesPageComponent, canActivate: [authGuard] },
  { path: 'settings', component: SettingsComponent, canActivate: [authGuard] },
  { path: 'admin/providers', component: AdminProvidersComponent, canActivate: [authGuard, adminGuard] },
  { path: 'admin/models', component: AdminModelsComponent, canActivate: [authGuard, adminGuard] },
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
