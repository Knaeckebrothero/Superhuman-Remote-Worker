import {TestBed} from '@angular/core/testing';
import {provideHttpClient} from '@angular/common/http';
import {provideHttpClientTesting} from '@angular/common/http/testing';
import {TranslocoService} from '@jsverse/transloco';
import {AdminUsageComponent} from './admin-usage.component';

// Note: uses TestBed.inject (not createComponent) to avoid JIT styleUrl
// resolution for AppIconComponent — the established pattern in this codebase
// when a component's deep dep has an external stylesheet. See admin-config.component.spec.ts.
describe('AdminUsageComponent refresh shell', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        AdminUsageComponent,
        provideHttpClient(),
        provideHttpClientTesting(),
        {provide: TranslocoService, useValue: {translate: (k: string) => k}},
      ],
    });
  });

  it('setRefresh updates the interval signal', () => {
    const c = TestBed.inject(AdminUsageComponent);
    expect(c.refreshIntervalMs()).toBe(0);
    c.setRefresh(30000);
    expect(c.refreshIntervalMs()).toBe(30000);
  });

  it('tokensTotal sums prompt + completion token quantities', () => {
    const c = TestBed.inject(AdminUsageComponent);
    (c as any).usage.usage.set({available: true, total_cost_usd: 0, by_category: [
      {category: 'llm', unit: 'prompt-token', quantity: 100, cost_usd: 0, events: 1},
      {category: 'llm', unit: 'completion-token', quantity: 25, cost_usd: 0, events: 1},
      {category: 'compute', unit: 'vcpu-hour', quantity: 2, cost_usd: 0, events: 1},
    ]});
    expect(c.tokensTotal()).toBe(125);
    expect(c.computeHours()).toBe(2);
  });
});
