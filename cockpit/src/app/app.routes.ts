import { Routes } from '@angular/router';
import { SettingsComponent } from './pages/settings/settings.component';
import { ShellPageComponent } from './simple/pages/shell/shell.component';
import { JobsPageComponent } from './simple/pages/jobs/jobs-page.component';
import { CreatePageComponent } from './simple/pages/create/create-page.component';
import { ReviewPageComponent } from './simple/pages/review/review-page.component';
import { DebugPageComponent } from './debug/pages/debug.component';
import { ProjectListPageComponent } from './shared/pages/project-list.component';
import { ProjectDetailPageComponent } from './pages/project-detail/project-detail.component';
import { SudoPageComponent } from './simple/pages/sudo/sudo-page.component';
import { authGuard } from './core/guards/auth.guard';

export const routes: Routes = [
  { path: '', component: ShellPageComponent, canActivate: [authGuard] },
  { path: 'jobs', component: JobsPageComponent, canActivate: [authGuard] },
  { path: 'create', component: CreatePageComponent, canActivate: [authGuard] },
  { path: 'review', component: ReviewPageComponent, canActivate: [authGuard] },
  { path: 'projects', component: ProjectListPageComponent, canActivate: [authGuard] },
  { path: 'projects/:id', component: ProjectDetailPageComponent, canActivate: [authGuard] },
  { path: 'settings', component: SettingsComponent, canActivate: [authGuard] },
  { path: 'sudo', component: SudoPageComponent, canActivate: [authGuard] },
  { path: 'debug', component: DebugPageComponent, canActivate: [authGuard] },
  { path: '**', redirectTo: '' },
];
