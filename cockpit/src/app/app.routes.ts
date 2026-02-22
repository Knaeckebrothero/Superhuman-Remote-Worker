import { Routes } from '@angular/router';
import { LoginComponent } from './pages/login/login.component';
import { ShellPageComponent } from './pages/shell/shell.component';
import { DebugPageComponent } from './pages/debug/debug.component';
import { ProjectListPageComponent } from './pages/project-list/project-list.component';
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
