import {WritableSignal, signal, ɵresolveComponentResources} from '@angular/core';
import {ComponentFixture, TestBed} from '@angular/core/testing';
import {TranslocoTestingModule} from '@jsverse/transloco';
import {afterEach, beforeAll, beforeEach, describe, expect, it} from 'vitest';
import {AppMultiSelectComponent, MultiSelectOption} from './multi-select.component';

/**
 * Harness notes — this repo's vitest pipeline never runs components through
 * ngtsc, so initializer-based APIs compile with no metadata (verified against
 * this very component; the same gap is documented in
 * views/contacts/contact-form.component.spec.ts):
 *
 *   - `fixture.componentRef.setInput('options', …)` logs NG0303 and does
 *     nothing, after which the required input read throws NG0950. Inputs are
 *     therefore seeded by REPLACING the InputSignal field with a plain
 *     `signal()` before the first change detection — the template and every
 *     computed only ever call `this.options()`, so they never notice.
 *   - `model()` / `output()` instances are built by their field initializers
 *     and work untouched, so `selected` is seeded with `.set()` and
 *     `selectionChange` is observed with `.subscribe()`.
 *   - Child signal-input bindings (`[checked]`, `[value]`) and child
 *     `output()` bindings are inert here too. The component deliberately
 *     listens to the *bubbling native* `change` / `input` events instead of
 *     app-checkbox's `(changed)` / app-input's `(valueChange)`, which is both
 *     the more robust wiring and what makes these assertions possible.
 */

const OPTIONS: MultiSelectOption[] = [
  {value: 'alpha', label: 'Alpha'},
  {value: 'bravo', label: 'Bravo'},
  {value: 'charlie', label: 'Charlie'},
  {value: 'delta', label: 'Delta'},
  {value: 'echo', label: 'Echo'},
];

const EN = {
  ui: {
    multiSelect: {
      placeholder: 'Select options',
      filterPlaceholder: 'Filter options',
      filterLabel: 'Filter options',
      empty: 'No matching options',
      selectedCount: '{{count}} selected',
      clear: 'Clear',
    },
  },
};

type SeedableInput = 'options' | 'label' | 'filterPlaceholder' | 'disabled' | 'maxSummaryItems';

function seed<T>(component: AppMultiSelectComponent, key: SeedableInput, value: T): void {
  (component as unknown as Record<SeedableInput, WritableSignal<T>>)[key] = signal(value);
}

interface Harness {
  fixture: ComponentFixture<AppMultiSelectComponent>;
  component: AppMultiSelectComponent;
  emitted: string[][];
}

let harness: Harness | null = null;

async function render(
  over: {selected?: string[]; label?: string; maxSummaryItems?: number} = {},
): Promise<Harness> {
  await TestBed.compileComponents();
  const fixture = TestBed.createComponent(AppMultiSelectComponent);
  const component = fixture.componentInstance;

  seed(component, 'options', OPTIONS);
  seed(component, 'label', over.label ?? 'Choose');
  if (over.maxSummaryItems !== undefined) {
    seed(component, 'maxSummaryItems', over.maxSummaryItems);
  }
  if (over.selected) component.selected.set(over.selected);

  const emitted: string[][] = [];
  component.selectionChange.subscribe((next) => emitted.push(next));

  fixture.detectChanges();
  harness = {fixture, component, emitted};
  return harness;
}

const trigger = (h: Harness) =>
  (h.fixture.nativeElement as HTMLElement).querySelector<HTMLButtonElement>(
    '.app-multi-select__trigger',
  )!;

const summaryText = (h: Harness) =>
  (h.fixture.nativeElement as HTMLElement)
    .querySelector('.app-multi-select__summary')
    ?.textContent?.trim();

/** The panel is portalled to <body> once opened, so it is found on `document`. */
const panel = () => document.querySelector<HTMLElement>('.app-multi-select__panel')!;
const filterInput = () => panel().querySelector<HTMLInputElement>('.app-multi-select__filter input')!;
const rowInputs = () =>
  Array.from(
    panel().querySelectorAll<HTMLInputElement>('.app-multi-select__option input[type="checkbox"]'),
  );
/** `.app-checkbox__content` is app-checkbox's projection slot — the box's ✓ lives outside it. */
const rowLabels = () =>
  Array.from(
    panel().querySelectorAll('.app-multi-select__option .app-checkbox__content'),
  ).map((slot) => slot.textContent?.trim());

function openPanel(h: Harness): void {
  trigger(h).click();
  h.fixture.detectChanges();
}

function type(h: Harness, text: string): void {
  const field = filterInput();
  field.value = text;
  field.dispatchEvent(new Event('input', {bubbles: true}));
  h.fixture.detectChanges();
}

function toggleRow(h: Harness, index: number, checked: boolean): void {
  const box = rowInputs()[index]!;
  box.checked = checked;
  box.dispatchEvent(new Event('change', {bubbles: true}));
  h.fixture.detectChanges();
}

/** ListKeyManager reads the legacy `keyCode`, which jsdom does not derive. */
function press(target: EventTarget, key: string, keyCode: number): void {
  target.dispatchEvent(new KeyboardEvent('keydown', {key, keyCode, bubbles: true}));
}

describe('AppMultiSelectComponent', () => {
  beforeAll(async () => {
    // app-multi-select, app-input and app-checkbox all use external styleUrls;
    // JIT needs the pending resource queue drained before TestBed can compile.
    await ɵresolveComponentResources(() => Promise.resolve(''));
  });

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [
        AppMultiSelectComponent,
        TranslocoTestingModule.forRoot({
          langs: {en: EN},
          translocoConfig: {availableLangs: ['en'], defaultLang: 'en'},
        }),
      ],
    });
  });

  afterEach(() => {
    harness?.fixture.destroy();
    harness = null;
    TestBed.resetTestingModule();
  });

  describe('selection summary', () => {
    it('falls back to the label when nothing is selected', async () => {
      const h = await render({label: 'Status'});
      expect(summaryText(h)).toBe('Status');
    });

    it('falls back to the translated placeholder when there is no label either', async () => {
      const h = await render({label: ''});
      expect(summaryText(h)).toBe('Select options');
    });

    it('names a single selection', async () => {
      const h = await render({selected: ['bravo']});
      expect(summaryText(h)).toBe('Bravo');
    });

    it('lists up to maxSummaryItems, then collapses the rest to +N', async () => {
      const three = await render({selected: ['alpha', 'bravo', 'charlie']});
      expect(summaryText(three)).toBe('Alpha, Bravo +1');
      three.fixture.destroy();
      harness = null;

      const five = await render({
        selected: ['alpha', 'bravo', 'charlie', 'delta', 'echo'],
      });
      expect(summaryText(five)).toBe('Alpha, Bravo +3');
    });

    it('honours a custom maxSummaryItems', async () => {
      const h = await render({
        selected: ['alpha', 'bravo', 'charlie', 'delta'],
        maxSummaryItems: 3,
      });
      expect(summaryText(h)).toBe('Alpha, Bravo, Charlie +1');
    });

    it('orders the summary by options(), not by the order values were selected', async () => {
      const h = await render({selected: ['charlie', 'alpha']});
      expect(summaryText(h)).toBe('Alpha, Charlie');
    });
  });

  describe('disclosure', () => {
    it('wires aria-expanded / aria-haspopup / aria-controls to the panel', async () => {
      const h = await render();
      const button = trigger(h);
      expect(button.getAttribute('aria-haspopup')).toBe('true');
      expect(button.getAttribute('aria-expanded')).toBe('false');
      expect(button.getAttribute('aria-controls')).toBe(panel().id);

      openPanel(h);
      expect(button.getAttribute('aria-expanded')).toBe('true');
    });

    it('portals the panel into <body> on open and removes it on destroy', async () => {
      const h = await render();
      openPanel(h);
      expect(panel().parentElement).toBe(document.body);

      h.fixture.destroy();
      harness = null;
      expect(document.querySelector('.app-multi-select__panel')).toBeNull();
    });

    it('moves focus to the filter on open', async () => {
      const h = await render();
      openPanel(h);
      expect(document.activeElement).toBe(filterInput());
    });

    it('closes when the disclosure button is pressed again', async () => {
      const h = await render();
      openPanel(h);
      expect(h.component.isOpen()).toBe(true);

      openPanel(h);
      expect(h.component.isOpen()).toBe(false);
      expect(trigger(h).getAttribute('aria-expanded')).toBe('false');
    });

    it('closes on an outside click', async () => {
      const h = await render();
      openPanel(h);

      document.body.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
      h.fixture.detectChanges();
      expect(h.component.isOpen()).toBe(false);
    });

    it('closes on Escape and returns focus to the disclosure button', async () => {
      const h = await render();
      openPanel(h);

      press(filterInput(), 'Escape', 27);
      h.fixture.detectChanges();

      expect(h.component.isOpen()).toBe(false);
      expect(document.activeElement).toBe(trigger(h));
    });
  });

  describe('toggling', () => {
    it('does NOT close the panel when an item is toggled', async () => {
      const h = await render();
      openPanel(h);

      toggleRow(h, 0, true);

      expect(h.component.isOpen()).toBe(true);
      expect(panel().hasAttribute('data-open')).toBe(true);
      expect(trigger(h).getAttribute('aria-expanded')).toBe('true');
    });

    it('emits the new array on every commit and keeps `selected` in step', async () => {
      const h = await render();
      openPanel(h);

      toggleRow(h, 0, true);
      expect(h.component.selected()).toEqual(['alpha']);

      toggleRow(h, 2, true);
      expect(h.component.selected()).toEqual(['alpha', 'charlie']);

      toggleRow(h, 0, false);
      expect(h.component.selected()).toEqual(['charlie']);

      expect(h.emitted).toEqual([['alpha'], ['alpha', 'charlie'], ['charlie']]);
    });

    it('clears the whole selection without closing the panel', async () => {
      const h = await render({selected: ['alpha', 'bravo']});
      openPanel(h);

      panel().querySelector<HTMLButtonElement>('.app-multi-select__clear')!.click();
      h.fixture.detectChanges();

      expect(h.component.selected()).toEqual([]);
      expect(h.emitted).toEqual([[]]);
      expect(h.component.isOpen()).toBe(true);
    });
  });

  describe('filtering', () => {
    it('narrows the list as you type, case-insensitively', async () => {
      const h = await render();
      openPanel(h);
      expect(rowLabels()).toEqual(['Alpha', 'Bravo', 'Charlie', 'Delta', 'Echo']);

      type(h, 'a');
      expect(rowLabels()).toEqual(['Alpha', 'Bravo', 'Charlie', 'Delta']);

      type(h, 'CHAR');
      expect(rowLabels()).toEqual(['Charlie']);
    });

    it('shows the empty state when the filter matches nothing', async () => {
      const h = await render();
      openPanel(h);

      type(h, 'zzz');

      expect(rowLabels()).toEqual([]);
      expect(panel().querySelector('.app-multi-select__empty')?.textContent?.trim()).toBe(
        'No matching options',
      );
    });

    it('toggles the right option after filtering', async () => {
      const h = await render();
      openPanel(h);
      type(h, 'char');

      toggleRow(h, 0, true);

      expect(h.component.selected()).toEqual(['charlie']);
    });

    it('resets the filter when the panel closes', async () => {
      const h = await render();
      openPanel(h);
      type(h, 'char');

      press(filterInput(), 'Escape', 27);
      h.fixture.detectChanges();
      openPanel(h);

      expect(rowLabels()).toEqual(['Alpha', 'Bravo', 'Charlie', 'Delta', 'Echo']);
    });
  });

  describe('keyboard', () => {
    it('ArrowDown from the filter enters the list, then walks it with a roving tabindex', async () => {
      const h = await render();
      openPanel(h);

      press(filterInput(), 'ArrowDown', 40);
      expect(document.activeElement).toBe(rowInputs()[0]);
      expect(rowInputs().map((el) => el.tabIndex)).toEqual([0, -1, -1, -1, -1]);

      press(rowInputs()[0]!, 'ArrowDown', 40);
      expect(document.activeElement).toBe(rowInputs()[1]);
      expect(rowInputs().map((el) => el.tabIndex)).toEqual([-1, 0, -1, -1, -1]);

      press(rowInputs()[1]!, 'ArrowUp', 38);
      expect(document.activeElement).toBe(rowInputs()[0]);
    });

    it('leaves typing in the filter alone — arrows only move the list once focus is in it', async () => {
      const h = await render();
      openPanel(h);

      // A printable key in the filter must not be swallowed as list typeahead.
      press(filterInput(), 'c', 67);
      type(h, 'c');
      expect(document.activeElement).toBe(filterInput());
      expect(rowLabels()).toEqual(['Charlie', 'Echo']);
    });
  });
});
