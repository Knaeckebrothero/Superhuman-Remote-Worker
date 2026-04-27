import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
} from '@angular/core';

export type FormFieldOrientation = 'vertical' | 'horizontal';

let nextFormFieldId = 0;

@Component({
  selector: 'app-form-field',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (label()) {
      <label class="app-form-field__label" [attr.for]="forId() || null">
        {{ label() }}
        @if (required()) {
          <span class="app-form-field__required" aria-hidden="true">*</span>
        }
        @if (optional()) {
          <span class="app-form-field__hint-inline">{{ optional() }}</span>
        }
      </label>
    }
    <div class="app-form-field__control">
      <ng-content></ng-content>
    </div>
    @if (error()) {
      <div class="app-form-field__error" role="alert" [attr.id]="errorId()">
        {{ error() }}
      </div>
    } @else if (hint()) {
      <div class="app-form-field__hint" [attr.id]="hintId()">
        {{ hint() }}
      </div>
    }
  `,
  styleUrl: './form-field.component.scss',
  host: {
    '[attr.data-orientation]': 'orientation()',
    '[attr.data-invalid]': 'error() ? "" : null',
  },
})
export class AppFormFieldComponent {
  label = input<string>('');
  required = input<boolean>(false);
  optional = input<string>('');
  hint = input<string>('');
  error = input<string>('');
  orientation = input<FormFieldOrientation>('vertical');
  forId = input<string>('');

  private readonly uid = ++nextFormFieldId;
  protected hintId = computed(() => (this.hint() ? `app-form-field-hint-${this.uid}` : null));
  protected errorId = computed(() => (this.error() ? `app-form-field-error-${this.uid}` : null));
}
