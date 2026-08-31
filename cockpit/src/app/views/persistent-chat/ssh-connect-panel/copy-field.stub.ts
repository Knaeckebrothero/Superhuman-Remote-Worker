import {Component, Input} from '@angular/core';

/**
 * TEST-ONLY stand-in for `<app-copy-field>`, exported under the real name so
 * the spec can `vi.mock('../../../ui/copy-field', () => import('./copy-field.stub'))`.
 *
 * The real component declares `value = input.required<string>()` and
 * `label = input<string>('')` (ui/copy-field/copy-field.component.ts). This
 * project's vitest pipeline never runs components through ngtsc, so
 * signal-input metadata is invisible to JIT: a property binding onto them
 * throws NG0303 (`ui/tool-card/icon-button.stub.ts` and
 * `cockpit_verification_gaps.md` document the same gap). The real
 * component's own template also nests `<app-icon-button>`/`<app-icon>`,
 * which have the identical problem one level deeper — mounting the real
 * thing here can't work regardless. Decorator inputs still bind, so this
 * stub renders where the real one cannot.
 *
 * `copyText` is re-exported unchanged: it is a plain function, not a
 * component, and needs no stubbing (exercised directly in copy-text.spec.ts).
 *
 * Not referenced by application code and therefore never bundled.
 */
@Component({
    selector: 'app-copy-field',
    standalone: true,
    template: `
      @if (label) {
        <span class="app-copy-field-stub__label">{{ label }}</span>
      }
      <span class="app-copy-field-stub__value">{{ value }}</span>
    `,
})
export class AppCopyFieldComponent {
    @Input() label = '';
    @Input() value = '';
}

export {copyText} from '../../../ui/copy-field/copy-text';
