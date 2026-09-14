import {CUSTOM_ELEMENTS_SCHEMA, ɵresolveComponentResources} from '@angular/core';
import {ComponentFixture, TestBed} from '@angular/core/testing';
import {TranslocoPipe, TranslocoTestingModule} from '@jsverse/transloco';
import {beforeAll, describe, expect, it} from 'vitest';

import {HelmManagedBadgeComponent} from './helm-managed-badge.component';

const EN = {
  admin: {
    helm: {
      badge: 'Helm',
      badgeDrift: 'Helm · overridden',
      tooltip: 'Declared in Helm values.',
      tooltipDrift: 'Edited here after Helm applied it.',
    },
  },
};

// The component is driven through `setInput`, and `app-badge` is left as an
// unknown element: the vitest JIT harness does not wire signal inputs (the
// badge's `tone` is one), so its host attributes cannot be asserted here —
// the attributes this component sets itself can. Same trap as directive
// output()s, see reference_directive_output_needs_decorator_in_specs.
describe('HelmManagedBadgeComponent', () => {
  beforeAll(async () => {
    // The component's declared imports still list app-badge (styleUrl);
    // resolve pending resources before TestBed compiles, like the admin specs.
    await ɵresolveComponentResources(() => Promise.resolve(''));
  });

  function setup(managed: boolean, drift = false): ComponentFixture<HelmManagedBadgeComponent> {
    TestBed.configureTestingModule({
      imports: [
        HelmManagedBadgeComponent,
        TranslocoTestingModule.forRoot({
          langs: {en: EN},
          translocoConfig: {availableLangs: ['en'], defaultLang: 'en'},
          preloadLangs: true,
        }),
      ],
    });
    TestBed.overrideComponent(HelmManagedBadgeComponent, {
      set: {imports: [TranslocoPipe], schemas: [CUSTOM_ELEMENTS_SCHEMA]},
    });
    const fixture = TestBed.createComponent(HelmManagedBadgeComponent);
    fixture.componentRef.setInput('managed', managed);
    fixture.componentRef.setInput('drift', drift);
    fixture.detectChanges();
    return fixture;
  }

  const badgeOf = (fixture: ComponentFixture<HelmManagedBadgeComponent>) =>
    fixture.nativeElement.querySelector('[data-testid="helm-managed-badge"]');

  it('renders nothing for an unmanaged row', () => {
    expect(badgeOf(setup(false))).toBeNull();
  });

  it('shows the Helm pill for a managed row', () => {
    const badge = badgeOf(setup(true));
    expect(badge).not.toBeNull();
    expect(badge.textContent.trim()).toBe('Helm');
    expect(badge.getAttribute('title')).toBe('Declared in Helm values.');
    expect(badge.getAttribute('data-drift')).toBeNull();
  });

  it('flags an admin override as drift', () => {
    const badge = badgeOf(setup(true, true));
    expect(badge.textContent.trim()).toBe('Helm · overridden');
    expect(badge.getAttribute('data-drift')).toBe('true');
    expect(badge.getAttribute('title')).toBe('Edited here after Helm applied it.');
  });
});
