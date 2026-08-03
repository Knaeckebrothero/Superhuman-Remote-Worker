import {Component, EventEmitter, Input, Output} from '@angular/core';

/**
 * TEST-ONLY stand-in for `<app-icon-button>`, exported under the real name so a
 * spec can `vi.mock('../icon-button', () => import('./icon-button.stub'))`.
 *
 * The real button declares `ariaLabel = input.required<string>()`
 * (icon-button.component.ts:50). This project's vitest pipeline drops
 * signal-input metadata — the gap notify-user-tool-card.spec.ts documents and
 * avoids by never rendering a result section — so the binding never lands and
 * reading it throws NG0950, which kills the whole card render. Decorator inputs
 * still bind, so this stub renders where the real one cannot.
 *
 * Lives in its own module because a `vi.mock` factory is hoisted above the
 * esbuild decorator helper, making a class defined inline there fail with
 * "__decorateClass is not a function".
 *
 * Not referenced by application code and therefore never bundled.
 */
@Component({
    selector: 'app-icon-button',
    standalone: true,
    template: '<button type="button" (click)="clicked.emit()"><ng-content /></button>',
})
export class AppIconButtonComponent {
    @Input() ariaLabel?: string;
    @Input() tooltip?: string;
    @Input() variant?: string;
    @Input() size?: string;
    @Input() disabled?: boolean;
    @Output() clicked = new EventEmitter<void>();
}
