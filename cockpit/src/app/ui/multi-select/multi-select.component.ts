import {
  ChangeDetectionStrategy,
  Component,
  DOCUMENT,
  ElementRef,
  OnDestroy,
  ViewChild,
  afterEveryRender,
  computed,
  inject,
  input,
  model,
  output,
  signal,
} from '@angular/core';
import {FocusKeyManager, type FocusableOption} from '@angular/cdk/a11y';
import {TranslocoPipe} from '@jsverse/transloco';
import type {Subscription} from 'rxjs';
import {AppCheckboxComponent} from '../checkbox';
import {AppInputComponent} from '../input';

export interface MultiSelectOption {
  value: string;
  label: string;
  disabled?: boolean;
}

/** Unique `aria-controls` target per instance. */
let nextPanelId = 0;

/**
 * FocusKeyManager option backed by a rendered checkbox row.
 *
 * The rows are `<app-checkbox>` instances, and `app-checkbox` exposes no
 * tabindex input — so roving tabindex has to be written onto the native input
 * it renders. Adapting that element directly (instead of querying for the
 * component with `viewChildren`) keeps focus movement and the roving tabindex
 * reading from the same source of truth.
 */
class MultiSelectRow implements FocusableOption {
  constructor(private readonly input: HTMLInputElement) {}

  get disabled(): boolean {
    return this.input.disabled;
  }

  focus(): void {
    this.input.focus();
  }

  setTabbable(tabbable: boolean): void {
    this.input.tabIndex = tabbable ? 0 : -1;
  }
}

/**
 * A checkbox group inside a disclosure panel, with filter-as-you-type.
 *
 * Deliberately NOT a combobox: the WAI-ARIA combobox pattern is single-select,
 * so "multi-select combobox" is off-pattern before it is written. This is the
 * shape MOJ's government filter ships and the one Adrian Roselli recommends —
 * a disclosure button that summarises the selection, opening a panel of plain
 * checkboxes that stays open while you tick things.
 *
 * `app-menu` cannot be reused for the panel because it closes on every item
 * activation; the portal/reposition/cleanup shape here is copied from it.
 */
@Component({
  selector: 'app-multi-select',
  standalone: true,
  imports: [TranslocoPipe, AppInputComponent, AppCheckboxComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <button
      #trigger
      type="button"
      class="app-multi-select__trigger"
      aria-haspopup="true"
      [disabled]="disabled()"
      [attr.aria-expanded]="isOpen()"
      [attr.aria-controls]="panelId"
      [attr.data-open]="isOpen() || null"
      (click)="toggle()"
    >
      <span class="app-multi-select__summary">{{
        summary() || label() || ('ui.multiSelect.placeholder' | transloco)
      }}</span>
      <span class="app-multi-select__chevron" aria-hidden="true">▾</span>
    </button>

    <div
      #panel
      class="app-multi-select__panel"
      role="group"
      [id]="panelId"
      [attr.aria-label]="label() || ('ui.multiSelect.placeholder' | transloco)"
      [attr.data-open]="isOpen() || null"
      (keydown)="onPanelKeydown($event)"
    >
      <app-input
        class="app-multi-select__filter"
        size="sm"
        autocomplete="off"
        [value]="filter()"
        [placeholder]="filterPlaceholder() || ('ui.multiSelect.filterPlaceholder' | transloco)"
        [ariaLabel]="'ui.multiSelect.filterLabel' | transloco"
        (input)="onFilterInput($event)"
      />

      <div class="app-multi-select__options">
        @for (option of visibleOptions(); track option.value) {
          <div class="app-multi-select__option">
            <app-checkbox
              size="sm"
              [checked]="isSelected(option.value)"
              [disabled]="option.disabled === true"
              (change)="onToggle(option, $event)"
              >{{ option.label }}</app-checkbox
            >
          </div>
        } @empty {
          <p class="app-multi-select__empty">{{ 'ui.multiSelect.empty' | transloco }}</p>
        }
      </div>

      <div class="app-multi-select__footer">
        <span class="app-multi-select__count" aria-live="polite">{{
          'ui.multiSelect.selectedCount' | transloco: {count: selected().length}
        }}</span>
        <button
          type="button"
          class="app-multi-select__clear"
          [disabled]="selected().length === 0"
          (click)="clear()"
        >
          {{ 'ui.multiSelect.clear' | transloco }}
        </button>
      </div>
    </div>
  `,
  styleUrl: './multi-select.component.scss',
})
export class AppMultiSelectComponent implements OnDestroy {
  readonly options = input.required<MultiSelectOption[]>();
  readonly selected = model<string[]>([]);
  /** Disclosure button label used while nothing is selected. */
  readonly label = input<string>('');
  readonly filterPlaceholder = input<string>('');
  readonly disabled = input<boolean>(false);
  /** Labels shown in the summary before it collapses to `… +N`. */
  readonly maxSummaryItems = input<number>(2);

  readonly selectionChange = output<string[]>();

  readonly isOpen = signal(false);

  protected readonly filter = signal('');
  protected readonly panelId = `app-multi-select-panel-${nextPanelId++}`;

  /**
   * Selected options in `options()` order — so the summary does not reshuffle
   * as the user ticks boxes in an arbitrary order.
   */
  private readonly selectedOptions = computed(() => {
    const chosen = new Set(this.selected());
    return this.options().filter((option) => chosen.has(option.value));
  });

  protected readonly summary = computed(() => {
    const chosen = this.selectedOptions();
    if (chosen.length === 0) return '';
    const max = Math.max(0, Math.trunc(this.maxSummaryItems()));
    const head = chosen
      .slice(0, max)
      .map((option) => option.label)
      .join(', ');
    const rest = chosen.length - Math.min(max, chosen.length);
    if (rest <= 0) return head;
    return head ? `${head} +${rest}` : `+${rest}`;
  });

  protected readonly visibleOptions = computed(() => {
    const needle = this.filter().trim().toLowerCase();
    const all = this.options();
    if (!needle) return all;
    return all.filter((option) => option.label.toLowerCase().includes(needle));
  });

  // `viewChild()` signal queries never resolve under this repo's vitest JIT
  // pipeline (no ngtsc — see multi-select.component.spec.ts), while decorator
  // queries do. Both resolve identically under AOT, so the panel portal stays
  // reachable in tests.
  @ViewChild('panel', {static: true}) protected panelRef?: ElementRef<HTMLElement>;
  @ViewChild('trigger', {static: true}) protected triggerRef?: ElementRef<HTMLButtonElement>;

  private readonly doc = inject(DOCUMENT);
  /** Stable array instance: FocusKeyManager re-reads it, so in-place edits land. */
  private readonly rows: MultiSelectRow[] = [];
  private keyManager?: FocusKeyManager<MultiSelectRow>;
  private keyManagerSub?: Subscription;

  private readonly onDocMouseDown = (event: MouseEvent) => {
    const target = event.target as Node | null;
    if (!target) return;
    if (this.panelRef?.nativeElement.contains(target)) return;
    // The trigger's own click handler toggles; closing here would re-open it.
    if (this.triggerRef?.nativeElement.contains(target)) return;
    this.close();
  };
  private readonly onDocKeydown = (event: KeyboardEvent) => {
    if (event.key !== 'Escape') return;
    event.preventDefault();
    this.close();
  };
  private readonly onScroll = () => this.reposition();
  private readonly onResize = () => this.reposition();

  constructor() {
    // Roving tabindex has to be written to DOM the component does not own.
    afterEveryRender(() => this.syncRows());
  }

  ngOnDestroy(): void {
    this.detach(false);
    const panel = this.panelRef?.nativeElement;
    if (panel && panel.parentElement === this.doc.body) {
      this.doc.body.removeChild(panel);
    }
  }

  open(): void {
    if (this.isOpen() || this.disabled()) return;
    const panel = this.panelRef?.nativeElement;
    if (panel && panel.parentElement !== this.doc.body) {
      this.doc.body.appendChild(panel);
    }
    this.isOpen.set(true);

    this.doc.addEventListener('mousedown', this.onDocMouseDown, true);
    this.doc.addEventListener('keydown', this.onDocKeydown, true);
    this.doc.addEventListener('scroll', this.onScroll, true);
    this.doc.defaultView?.addEventListener('resize', this.onResize);

    // reposition() force-sets [data-open] before change detection applies the
    // binding, so the panel is laid out (not display:none) and focusable now.
    this.reposition();
    this.syncRows();
    this.focusFilter();
  }

  close(): void {
    this.detach(true);
  }

  toggle(): void {
    if (this.isOpen()) {
      this.close();
    } else {
      this.open();
    }
  }

  protected isSelected(value: string): boolean {
    return this.selected().includes(value);
  }

  protected onToggle(option: MultiSelectOption, event: Event): void {
    const checked = (event.target as HTMLInputElement).checked;
    const current = this.selected();
    if (checked === current.includes(option.value)) return;
    this.commit(
      checked
        ? [...current, option.value]
        : current.filter((value) => value !== option.value),
    );
  }

  protected clear(): void {
    if (this.selected().length === 0) return;
    this.commit([]);
    // The Clear button disables itself on an empty selection, which would drop
    // focus to <body>; hand it back to the filter instead.
    this.focusFilter();
  }

  /** Reads the bubbling native `input` event rather than app-input's output. */
  protected onFilterInput(event: Event): void {
    this.filter.set((event.target as HTMLInputElement).value);
  }

  protected onPanelKeydown(event: KeyboardEvent): void {
    // Escape is handled once, on the document, so it also works when focus has
    // drifted outside the panel.
    if (event.key === 'Escape') return;

    // The DOM is current at keydown time, whatever the render scheduler did.
    this.syncRows();
    const keyManager = this.ensureKeyManager();
    const target = event.target as HTMLElement | null;

    if (target?.closest('.app-multi-select__option')) {
      // Focus is in the list: arrows belong to the list, not the filter.
      keyManager.onKeydown(event);
      return;
    }
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      keyManager.setFirstItemActive();
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      keyManager.setLastItemActive();
    }
  }

  private commit(next: string[]): void {
    this.selected.set(next);
    this.selectionChange.emit(next);
  }

  private detach(restoreFocus: boolean): void {
    if (!this.isOpen()) return;
    this.isOpen.set(false);
    this.filter.set('');
    this.doc.removeEventListener('mousedown', this.onDocMouseDown, true);
    this.doc.removeEventListener('keydown', this.onDocKeydown, true);
    this.doc.removeEventListener('scroll', this.onScroll, true);
    this.doc.defaultView?.removeEventListener('resize', this.onResize);
    this.keyManagerSub?.unsubscribe();
    this.keyManagerSub = undefined;
    this.keyManager?.destroy();
    this.keyManager = undefined;
    this.rows.length = 0;
    if (restoreFocus) this.triggerRef?.nativeElement.focus();
  }

  private ensureKeyManager(): FocusKeyManager<MultiSelectRow> {
    if (!this.keyManager) {
      this.keyManager = new FocusKeyManager<MultiSelectRow>(this.rows)
        .withWrap()
        .withVerticalOrientation()
        .withHomeAndEnd();
      // Arrow movement has to drag the roving tabindex along with focus.
      this.keyManagerSub = this.keyManager.change.subscribe((index) => this.applyRoving(index));
    }
    return this.keyManager;
  }

  private focusFilter(): void {
    this.panelRef?.nativeElement
      .querySelector<HTMLInputElement>('.app-multi-select__filter input')
      ?.focus();
  }

  /** Rebuild the FocusKeyManager rows and re-apply the roving tabindex. */
  private syncRows(): void {
    const panel = this.panelRef?.nativeElement;
    if (!panel || !this.isOpen()) {
      this.rows.length = 0;
      return;
    }
    const inputs = Array.from(
      panel.querySelectorAll<HTMLInputElement>(
        '.app-multi-select__option input[type="checkbox"]',
      ),
    );
    this.rows.length = 0;
    for (const input of inputs) this.rows.push(new MultiSelectRow(input));

    // Filtering can shrink the list out from under the active index.
    const active = this.keyManager?.activeItemIndex ?? -1;
    const roving = this.clampRoving(active);
    if (this.keyManager && active !== roving) this.keyManager.updateActiveItem(roving);
    this.applyRoving(roving);
  }

  private clampRoving(index: number): number {
    if (this.rows.length === 0) return -1;
    return Math.min(Math.max(index, 0), this.rows.length - 1);
  }

  private applyRoving(index: number): void {
    const roving = this.clampRoving(index);
    this.rows.forEach((row, i) => row.setTabbable(i === roving));
  }

  private reposition(): void {
    const panel = this.panelRef?.nativeElement;
    const anchor = this.triggerRef?.nativeElement;
    if (!panel || !anchor || !this.isOpen()) return;
    if (!panel.hasAttribute('data-open')) panel.setAttribute('data-open', '');

    const margin = 4;
    const a = anchor.getBoundingClientRect();

    panel.style.visibility = 'hidden';
    panel.style.left = '0px';
    panel.style.top = '0px';
    panel.style.minWidth = `${Math.round(a.width)}px`;
    const p = panel.getBoundingClientRect();

    const view = this.doc.defaultView;
    const viewW = view?.innerWidth ?? 1024;
    const viewH = view?.innerHeight ?? 768;

    let left = a.left;
    let top = a.bottom + margin;
    // Flip above the trigger when the panel would not fit below it.
    if (top + p.height > viewH - 8 && a.top - p.height - margin >= 8) {
      top = a.top - p.height - margin;
    }
    left = Math.max(8, Math.min(left, viewW - p.width - 8));
    top = Math.max(8, Math.min(top, viewH - p.height - 8));

    panel.style.left = `${Math.round(left)}px`;
    panel.style.top = `${Math.round(top)}px`;
    panel.style.visibility = 'visible';
  }
}
