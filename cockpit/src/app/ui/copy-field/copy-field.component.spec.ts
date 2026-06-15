import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {Injector, runInInjectionContext} from '@angular/core';

// Isolate the component's glue (copied-state toggle + reset) from the real
// clipboard side effect; copy-text.spec.ts covers copyText itself.
vi.mock('./copy-text', () => ({copyText: vi.fn()}));
import {copyText} from './copy-text';
import {AppCopyFieldComponent} from './copy-field.component';

const copyTextMock = vi.mocked(copyText);

/**
 * Instantiate in a bare injection context (no template render), matching the
 * repo's component-unit convention. `copy(text)` takes the value as an argument
 * so we never need to set the `value` input() signal.
 */
function createComponent(): AppCopyFieldComponent {
  const injector = Injector.create({providers: []});
  return runInInjectionContext(injector, () => new AppCopyFieldComponent());
}

describe('AppCopyFieldComponent', () => {
  beforeEach(() => copyTextMock.mockReset());
  afterEach(() => vi.useRealTimers());

  it('copies the provided value and flips copied to true', async () => {
    copyTextMock.mockResolvedValue(true);
    const component = createComponent();
    expect(component.copied()).toBe(false);

    await component.copy('job-5cc78c52-full-uuid');

    expect(copyTextMock).toHaveBeenCalledWith('job-5cc78c52-full-uuid');
    expect(component.copied()).toBe(true);
  });

  it('leaves copied false when the copy fails', async () => {
    copyTextMock.mockResolvedValue(false);
    const component = createComponent();

    await component.copy('job-5cc78c52-full-uuid');

    expect(component.copied()).toBe(false);
  });

  it('resets copied to false after the timeout', async () => {
    vi.useFakeTimers();
    copyTextMock.mockResolvedValue(true);
    const component = createComponent();

    await component.copy('job-5cc78c52-full-uuid');
    expect(component.copied()).toBe(true);

    vi.advanceTimersByTime(2500);
    expect(component.copied()).toBe(false);
  });
});
