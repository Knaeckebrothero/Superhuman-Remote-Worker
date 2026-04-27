import {
  AfterViewInit,
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  effect,
  inject,
  input,
  model,
  output,
  viewChild,
} from '@angular/core';
import { FocusMonitor } from '@angular/cdk/a11y';

export type SelectSize = 'sm' | 'md' | 'lg';

@Component({
  selector: 'app-select',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <select
      #selectEl
      class="app-select__field"
      [disabled]="disabled()"
      [required]="required()"
      [attr.aria-label]="ariaLabel() || null"
      [attr.aria-invalid]="invalid() || null"
      [attr.data-size]="size()"
      [attr.data-invalid]="invalid() || null"
      (change)="onChange($event)"
      (blur)="blurred.emit($event)"
      (focus)="focused.emit($event)"
    >
      <ng-content></ng-content>
    </select>
    <span class="app-select__chevron" aria-hidden="true">▾</span>
  `,
  styleUrl: './select.component.scss',
  host: {
    '[attr.data-full-width]': 'fullWidth() || null',
  },
})
export class AppSelectComponent<T = string> implements AfterViewInit {
  value = model<T | null>(null);

  size = input<SelectSize>('md');
  disabled = input<boolean>(false);
  required = input<boolean>(false);
  invalid = input<boolean>(false);
  fullWidth = input<boolean>(true);
  ariaLabel = input<string>('');

  changed = output<T | null>();
  focused = output<FocusEvent>();
  blurred = output<FocusEvent>();

  private selectEl = viewChild.required<ElementRef<HTMLSelectElement>>('selectEl');
  private focusMonitor = inject(FocusMonitor);
  private host = inject(ElementRef<HTMLElement>);

  constructor() {
    this.focusMonitor.monitor(this.host.nativeElement, true);
    // Sync model value → DOM. Native <select> needs its `value` set imperatively
    // because the options live in projected content.
    effect(() => {
      const el = this.selectEl?.()?.nativeElement;
      if (!el) return;
      const next = this.value();
      const str = next == null ? '' : String(next);
      if (el.value !== str) el.value = str;
    });
  }

  ngAfterViewInit() {
    // Ensure value is applied once projected options are present.
    const el = this.selectEl().nativeElement;
    const next = this.value();
    el.value = next == null ? '' : String(next);
  }

  ngOnDestroy() {
    this.focusMonitor.stopMonitoring(this.host.nativeElement);
  }

  focus() {
    this.selectEl().nativeElement.focus();
  }

  protected onChange(event: Event) {
    const next = (event.target as HTMLSelectElement).value as unknown as T;
    this.value.set(next);
    this.changed.emit(next);
  }
}
