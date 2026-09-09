import {afterEach, beforeAll, describe, expect, it} from 'vitest';
import {Injector, runInInjectionContext, ɵresolveComponentResources} from '@angular/core';
import {TestBed} from '@angular/core/testing';
import {provideRouter} from '@angular/router';
import {TranslocoService, TranslocoTestingModule} from '@jsverse/transloco';
import {ChatEmptyStateComponent} from './chat-empty-state.component';
import en from '../../../../assets/i18n/en.json';

function create() {
  const injector = Injector.create({providers: []});
  return runInInjectionContext(injector, () => new ChatEmptyStateComponent());
}

describe('ChatEmptyStateComponent', () => {
  it('re-emits a picked suggestion to the parent', () => {
    const component = create();
    const seen: unknown[] = [];
    component.suggestionPicked.subscribe((s) => seen.push(s));

    component.pick({icon: 'route', text: 'Review the manifest'});

    expect(seen).toEqual([{icon: 'route', text: 'Review the manifest'}]);
  });

  it('re-emits the connector toggle as a boolean', () => {
    const component = create();
    const seen: boolean[] = [];
    component.connectorsToggled.subscribe((v) => seen.push(v));

    component.onConnectorsToggle({target: {checked: true}} as unknown as Event);

    expect(seen).toEqual([true]);
  });
});

// Rendering assertions need the real template compiled (variant switches
// markup, not just data), so TestBed rather than the Injector.create
// construction above — same split as cloud-review-banner.component.spec.ts,
// this component's sibling extracted for the same CSS-budget reason.
describe('ChatEmptyStateComponent variants', () => {
  beforeAll(async () => {
    await ɵresolveComponentResources(() => Promise.resolve(''));
  });

  afterEach(() => TestBed.resetTestingModule());

  function render(inputs: Record<string, unknown>): HTMLElement {
    TestBed.configureTestingModule({
      imports: [
        ChatEmptyStateComponent,
        TranslocoTestingModule.forRoot({
          langs: {en},
          translocoConfig: {availableLangs: ['en'], defaultLang: 'en'},
        }),
      ],
      providers: [provideRouter([])],
    });
    const transloco = TestBed.inject(TranslocoService);
    transloco.setTranslation(en, 'en');
    transloco.setActiveLang('en');
    const fixture = TestBed.createComponent(ChatEmptyStateComponent);
    // Assigned rather than setInput() — this vitest pipeline drops
    // signal-input metadata (see job-tool-card-panel.spec.ts).
    const inst = fixture.componentInstance as unknown as Record<string, unknown>;
    for (const [k, v] of Object.entries(inputs)) inst[k] = () => v;
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  const text = (root: HTMLElement) => (root.textContent ?? '').replace(/\s+/g, ' ').trim();

  it('renders neither the connectors control nor the advanced link for "ready"', () => {
    const root = render({variant: 'ready', suggestions: []});
    expect(root.querySelector('.draft-connectors')).toBeNull();
    expect(root.querySelector('.draft-advanced')).toBeNull();
    // The other half of "two i18n keys": ready's own copy, not draft's.
    expect(text(root)).toContain('The agent is connected and listening');
  });

  it('renders both the connectors control and the advanced link for "draft"', () => {
    const root = render({
      variant: 'draft',
      suggestions: [],
      connectorsEnabled: true,
      datasourceCount: 2,
    });
    expect(root.querySelector('.draft-connectors')).not.toBeNull();
    expect(root.querySelector('.draft-advanced')).not.toBeNull();
    expect(text(root)).toContain('Just start typing');
    expect(text(root)).toContain('Default connectors (2)');
  });

  it('still renders suggestion chips for either variant', () => {
    const root = render({
      variant: 'ready',
      suggestions: [{icon: 'route', text: 'Review the manifest'}],
    });
    const chip = root.querySelector<HTMLButtonElement>('.suggestion-chip');
    expect(chip?.textContent).toContain('Review the manifest');
  });
});
