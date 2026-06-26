import {TestBed} from '@angular/core/testing';
import {provideHttpClient} from '@angular/common/http';
import {provideHttpClientTesting} from '@angular/common/http/testing';
import {AdminUsageComponent} from './admin-usage.component';

// Note: uses TestBed.inject (not createComponent) to avoid JIT styleUrl
// resolution for AppIconComponent — the established pattern in this codebase
// when a component's deep dep has an external stylesheet. See admin-config.component.spec.ts.
describe('AdminUsageComponent refresh shell', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [AdminUsageComponent, provideHttpClient(), provideHttpClientTesting()],
    });
  });

  it('setRefresh updates the interval signal', () => {
    const c = TestBed.inject(AdminUsageComponent);
    expect(c.refreshIntervalMs()).toBe(0);
    c.setRefresh(30000);
    expect(c.refreshIntervalMs()).toBe(30000);
  });
});
