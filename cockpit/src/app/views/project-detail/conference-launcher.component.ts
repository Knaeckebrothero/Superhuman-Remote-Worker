import {Component, inject, OnInit, signal} from '@angular/core';
import {ActivatedRoute, Router} from '@angular/router';
import {HttpClient} from '@angular/common/http';
import {firstValueFrom} from 'rxjs';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';
import {ApiService} from '../../core/services/api.service';
import {openOrResumeConference} from '../../core/officer/conference';
import {AppButtonComponent} from '../../ui/button';
import {AppSpinnerComponent} from '../../ui/spinner';

/**
 * `/projects/:id/officer/conference` — the one door into the officer's
 * conference (officer_visibility_streamline.md §3.5). Resolves
 * create-or-resume and replaces itself with the session, so Back never
 * re-runs it. Reached from the notification's `open_conference` action,
 * the sessions list's Talk button, and any deep link.
 */
@Component({
  selector: 'app-conference-launcher',
  standalone: true,
  imports: [TranslocoPipe, AppButtonComponent, AppSpinnerComponent],
  template: `
    <div class="launcher" data-testid="conference-launcher">
      @if (error(); as e) {
        <p class="launcher-error" role="alert">
          {{ 'conferenceLauncher.error' | transloco }} {{ e }}
        </p>
        <app-button variant="secondary" size="sm" (clicked)="back()">
          {{ 'conferenceLauncher.back' | transloco }}
        </app-button>
      } @else {
        <app-spinner />
        <p>{{ 'conferenceLauncher.opening' | transloco }}</p>
      }
    </div>
  `,
  styles: [
    `
      .launcher {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 12px;
        padding: 48px 16px;
        color: var(--text-muted);
      }
      .launcher-error {
        color: var(--text);
      }
    `,
  ],
})
export class ConferenceLauncherComponent implements OnInit {
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly http = inject(HttpClient);
  private readonly api = inject(ApiService);
  private readonly transloco = inject(TranslocoService);

  readonly error = signal<string | null>(null);

  async ngOnInit(): Promise<void> {
    const projectId = this.route.snapshot.paramMap.get('id') ?? '';
    if (!projectId) {
      this.error.set(this.transloco.translate('conferenceLauncher.missingProject'));
      return;
    }
    const project = await firstValueFrom(this.api.getProject(projectId)).catch(() => null);
    const result = await openOrResumeConference(
      {http: this.http, getOfficerPost: (id) => this.api.getOfficerPost(id)},
      projectId,
      project?.name || this.transloco.translate('officerCard.defaults.project'),
      this.transloco.translate('officerCard.actions.conference'),
    );
    if (result.kind === 'opened') {
      await this.router.navigate(['/sessions', result.threadId], {replaceUrl: true});
      return;
    }
    this.error.set(result.detail || String(result.status));
  }

  back(): void {
    void this.router.navigate(['/projects', this.route.snapshot.paramMap.get('id')]);
  }
}
