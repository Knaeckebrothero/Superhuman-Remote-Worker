import { Routes } from '@angular/router';
import { LoginComponent } from './pages/login/login.component';
import { ShellPageComponent } from './simple/pages/shell/shell.component';
import { DebugPageComponent } from './debug/pages/debug.component';
import { ProjectListPageComponent } from './shared/pages/project-list.component';
import { ProjectDetailPageComponent } from './pages/project-detail/project-detail.component';
import { authGuard } from './core/guards/auth.guard';

export const routes: Routes = [
  { path: 'login', component: LoginComponent },
  { path: '', component: ShellPageComponent, canActivate: [authGuard] },
  { path: 'debug', component: DebugPageComponent, canActivate: [authGuard] },
  { path: 'projects', component: ProjectListPageComponent, canActivate: [authGuard] },
  { path: 'projects/:id', component: ProjectDetailPageComponent, canActivate: [authGuard] },
  { path: '**', redirectTo: '' },
];
