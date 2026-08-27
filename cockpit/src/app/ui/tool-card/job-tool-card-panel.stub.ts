import {Component, EventEmitter, Input, Output} from '@angular/core';
import {ToolCardEntity} from '../../core/models/tool-card.model';

/**
 * TEST-ONLY stand-in for `<app-job-tool-card-panel>`, exported under the real
 * name so a spec can
 * `vi.mock('./job-tool-card-panel.component', () => import('./job-tool-card-panel.stub'))`.
 *
 * The real panel declares `entity = input.required<ToolCardEntity>()` and reads
 * it inside a constructor `effect()`. This project's vitest pipeline drops
 * signal-input metadata — the gap `icon-button.stub.ts` documents — so the
 * binding never lands and the effect throws NG0950, taking the whole parent
 * render with it. That happens whether the input is set via
 * `componentRef.setInput` or bound from a parent template, which is why
 * `job-batch-card.spec.ts` needs this and `job-tool-card-panel.spec.ts` (which
 * assigns the field directly) does not.
 *
 * Decorator inputs still bind, so this renders where the real one cannot. The
 * panel's own behaviour is covered by `job-tool-card-panel.spec.ts` and was
 * live-gated on k3d; nothing here asserts on its markup.
 *
 * Not referenced by application code and therefore never bundled.
 */
@Component({
    selector: 'app-job-tool-card-panel',
    standalone: true,
    template: '<span class="jc-stub">{{ entity?.id }}</span>',
})
export class JobToolCardPanelComponent {
    @Input() entity?: ToolCardEntity;
    @Output() diffRequested = new EventEmitter<string>();
}
