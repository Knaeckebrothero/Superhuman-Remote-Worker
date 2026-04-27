import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  HostListener,
  computed,
  inject,
  input,
} from '@angular/core';
import {FocusableOption} from '@angular/cdk/a11y';
import {AppRadioGroupComponent} from './radio-group.component';

@Component({
  selector: 'app-radio',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <span class="app-radio__circle" aria-hidden="true">
      <span class="app-radio__dot"></span>
    </span>
    <span class="app-radio__content"><ng-content></ng-content></span>
  `,
  styleUrl: './radio.component.scss',
  host: {
    role: 'radio',
    '[attr.aria-checked]': 'isActive()',
    '[attr.aria-disabled]': 'isEffectivelyDisabled() || null',
    '[attr.tabindex]': 'tabIndex()',
    '[attr.data-active]': 'isActive() || null',
    '[attr.data-disabled]': 'isEffectivelyDisabled() || null',
  },
})
export class AppRadioComponent<T = unknown> implements FocusableOption {
  value = input.required<T>();
  isDisabled = input<boolean>(false, {alias: 'disabled'});

  get disabled(): boolean {
    return this.isEffectivelyDisabled();
  }

  private group = inject(AppRadioGroupComponent, {optional: true});
  private host = inject(ElementRef<HTMLElement>);

  protected isActive = computed(() => this.group?.value() === this.value());
  protected isEffectivelyDisabled = computed(
    () => this.isDisabled() || (this.group?.disabled() ?? false),
  );

  // Roving tabindex: only the active radio is tabbable from outside.
  // If nothing is selected yet, the first radio gets tabindex=0 so the group
  // remains keyboard-reachable.
  protected tabIndex = computed(() => {
    if (this.isEffectivelyDisabled()) return -1;
    if (this.isActive()) return 0;
    const groupValue = this.group?.value();
    if (groupValue == null) {
      const items = this.group?.items() ?? [];
      const firstEnabled = items.find((r) => !r.isDisabled());
      return firstEnabled === this ? 0 : -1;
    }
    return -1;
  });

  focus(): void {
    this.host.nativeElement.focus();
  }

  getLabel(): string {
    return this.host.nativeElement.textContent?.trim() ?? '';
  }

  @HostListener('click')
  protected onClick(): void {
    if (this.isEffectivelyDisabled()) return;
    this.group?.select(this.value());
  }

  @HostListener('keydown.space', ['$event'])
  @HostListener('keydown.enter', ['$event'])
  protected onSelectKey(event: Event): void {
    if (this.isEffectivelyDisabled()) return;
    event.preventDefault();
    this.group?.select(this.value());
  }
}
