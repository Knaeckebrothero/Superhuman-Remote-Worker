import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  inject,
  input,
  model,
  output,
  viewChild,
} from '@angular/core';
import { FocusMonitor } from '@angular/cdk/a11y';

export type CheckboxSize = 'sm' | 'md';

@Component({
  selector: 'app-checkbox',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <label
      class="app-checkbox__label"
      [attr.data-size]="size()"
      [attr.data-disabled]="disabled() || null"
    >
      <input
        #inputEl
        type="checkbox"
        class="app-checkbox__input"
        [checked]="checked()"
        [disabled]="disabled()"
        [attr.aria-label]="ariaLabel() || null"
        (change)="onChange($event)"
        (blur)="blurred.emit($event)"
        (focus)="focused.emit($event)"
      />
      <span class="app-checkbox__box" aria-hidden="true">
        <span class="app-checkbox__check">✓</span>
      </span>
      <span class="app-checkbox__content">
        <ng-content></ng-content>
      </span>
    </label>
  `,
  styleUrl: './checkbox.component.scss',
})
export class AppCheckboxComponent {
  checked = model<boolean>(false);

  size = input<CheckboxSize>('md');
  disabled = input<boolean>(false);
  ariaLabel = input<string>('');

  changed = output<boolean>();
  focused = output<FocusEvent>();
  blurred = output<FocusEvent>();

  private inputEl = viewChild.required<ElementRef<HTMLInputElement>>('inputEl');
  private focusMonitor = inject(FocusMonitor);
  private host = inject(ElementRef<HTMLElement>);

  constructor() {
    this.focusMonitor.monitor(this.host.nativeElement, true);
  }

  ngOnDestroy() {
    this.focusMonitor.stopMonitoring(this.host.nativeElement);
  }

  focus() {
    this.inputEl().nativeElement.focus();
  }

  protected onChange(event: Event) {
    const next = (event.target as HTMLInputElement).checked;
    this.checked.set(next);
    this.changed.emit(next);
  }
}
