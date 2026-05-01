import {ChangeDetectionStrategy, Component, computed, inject, OnInit} from '@angular/core';
import {RouterLink} from '@angular/router';
import {ReadinessService} from '../../core/services/readiness.service';
import {UserService} from '../../core/services/user.service';

/**
 * Persistent top banner that reads ``GET /api/system/readiness`` and shows
 * the three-step onboarding checklist when the LLM stack isn't ready:
 *
 *   1. At least one provider key or endpoint configured.
 *   2. At least one chat / embedding / auxiliary catalog row.
 *   3. A default model pinned for each required capability.
 *
 * Each step deep-links to the admin page that resolves it. Non-admin users
 * see a "contact your admin" message instead — the underlying readiness
 * data is the same, but they can't fix it themselves.
 *
 * The banner is the cockpit-side companion to the dispatcher's 503 hard-
 * fail (`/api/jobs` and `/api/persistent/threads`): together they replace
 * the v1 path where the cockpit happily created a session but dispatch
 * silently failed at api.openai.com with `not-needed`.
 */
@Component({
  selector: 'app-readiness-gate-banner',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  template: `
    @if (shouldShow()) {
      <div class="readiness-banner" role="alert">
        <div class="banner-header">
          <strong>System not ready.</strong>
          @if (isAdmin()) {
            <span>Finish these steps to enable jobs and sessions:</span>
          } @else {
            <span>Ask an administrator to finish setup before launching jobs or sessions.</span>
          }
        </div>
        @if (isAdmin()) {
          <ol class="banner-steps">
            <li [class.done]="hasProvider()">
              @if (hasProvider()) {
                ✓
              } @else {
                <a routerLink="/admin/providers" class="banner-link">Configure a provider key or endpoint</a>
              }
              @if (hasProvider()) {
                <span>Provider configured</span>
              }
            </li>
            <li [class.done]="hasAllCapabilities()">
              @if (hasAllCapabilities()) {
                ✓ <span>Models added for chat, embedding, auxiliary</span>
              } @else {
                <a routerLink="/admin/models" class="banner-link">
                  Add models for: {{ missingCapabilitiesText() }}
                </a>
              }
            </li>
            <li [class.done]="hasAllDefaults()">
              @if (hasAllDefaults()) {
                ✓ <span>Defaults pinned</span>
              } @else if (missingDefaultsText()) {
                <a routerLink="/admin/providers" fragment="defaults" class="banner-link">
                  Pin a default for: {{ missingDefaultsText() }}
                </a>
              } @else {
                <span>(Pinning defaults available once each capability has a model.)</span>
              }
            </li>
          </ol>
        }
      </div>
    }
  `,
  styles: [`
    .readiness-banner {
      padding: 12px 16px;
      background: rgba(243, 139, 168, 0.12);
      border-bottom: 1px solid rgba(243, 139, 168, 0.4);
      color: var(--red, #f38ba8);
      font-size: 13px;
      line-height: 1.5;
    }
    .banner-header {
      display: flex;
      gap: 8px;
      align-items: baseline;
      flex-wrap: wrap;
    }
    .banner-steps {
      margin: 8px 0 0 22px;
      padding: 0;
    }
    .banner-steps li {
      margin: 4px 0;
    }
    .banner-steps li.done {
      color: var(--text-muted);
    }
    .banner-link {
      color: inherit;
      font-weight: 600;
      text-decoration: underline;
      cursor: pointer;
      margin-right: 6px;
    }
  `],
})
export class ReadinessGateBannerComponent implements OnInit {
  private readonly readiness = inject(ReadinessService);
  private readonly userService = inject(UserService);

  readonly isAdmin = computed(
    () => this.userService.currentUser()?.is_admin === true,
  );

  readonly shouldShow = computed(() => {
    if (!this.userService.isAuthenticated()) return false;
    if (!this.readiness.loaded()) return false;
    return this.readiness.readiness().ready === false;
  });

  readonly hasProvider = computed(() => {
    return this.readiness.readiness().missing_providers.length === 0;
  });

  readonly hasAllCapabilities = computed(() => {
    return this.readiness.readiness().missing_capabilities.length === 0;
  });

  readonly hasAllDefaults = computed(() => {
    return this.readiness.readiness().missing_defaults.length === 0;
  });

  readonly missingCapabilitiesText = computed(() => {
    return this.readiness.readiness().missing_capabilities.join(', ');
  });

  readonly missingDefaultsText = computed(() => {
    return this.readiness.readiness().missing_defaults.join(', ');
  });

  ngOnInit(): void {
    this.readiness.load();
  }
}
