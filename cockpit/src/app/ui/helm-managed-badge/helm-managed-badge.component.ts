import {ChangeDetectionStrategy, Component, Input} from '@angular/core';
import {TranslocoPipe} from '@jsverse/transloco';

import {AppBadgeComponent} from '../badge/badge.component';

/**
 * "Helm" pill for provider rows the deployment's `llm.seed` declares with
 * `reconcile: true`. The next `helm upgrade` re-applies such a row, so an
 * admin edit made in the Cockpit is temporary — `drift` marks exactly that
 * state (the row was last written here, not by the seed Job).
 *
 * Renders nothing for unmanaged rows, so it can sit unconditionally in a
 * row template.
 */
@Component({
  selector: 'app-helm-managed-badge',
  standalone: true,
  imports: [AppBadgeComponent, TranslocoPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (managed) {
      <app-badge
        [tone]="drift ? 'warning' : 'info'"
        size="sm"
        shape="pill"
        [title]="(drift ? 'admin.helm.tooltipDrift' : 'admin.helm.tooltip') | transloco"
        data-testid="helm-managed-badge"
        [attr.data-drift]="drift || null"
      >
        {{ (drift ? 'admin.helm.badgeDrift' : 'admin.helm.badge') | transloco }}
      </app-badge>
    }
  `,
})
export class HelmManagedBadgeComponent {
  // Decorator inputs on purpose: the vitest JIT harness does not wire signal
  // inputs on a component under test (same trap as directive output()s —
  // see reference_directive_output_needs_decorator_in_specs).
  /** `managed_by_helm` from the API row. */
  @Input() managed: boolean | null | undefined = false;
  /** `helm_drift` from the API row: edited here since Helm last applied it. */
  @Input() drift: boolean | null | undefined = false;
}
