import type { FullResult, Reporter, Suite, TestCase, TestResult } from '@playwright/test/reporter';

const JOURNEY_PROJECT = 'app-chromium';

function isJourney(test: TestCase): boolean {
  return test.parent.project()?.name === JOURNEY_PROJECT;
}

export default class NonEmptyJourneyReporter implements Reporter {
  private discovered = 0;
  private executed = 0;
  private readonly listing = process.argv.includes('--list');

  printsToStdio(): boolean {
    return false;
  }

  onBegin(_config: unknown, suite: Suite): void {
    this.discovered = suite.allTests().filter(isJourney).length;
  }

  onTestEnd(test: TestCase, result: TestResult): void {
    if (isJourney(test) && result.status !== 'skipped') this.executed += 1;
  }

  async onEnd(_result: FullResult): Promise<{ status?: FullResult['status'] } | undefined> {
    // Discovery is intentionally useful without credentials or a running
    // stack. The executable gate, however, must never turn zero/all-skipped
    // journey tests into green setup-only evidence.
    if (this.listing) return undefined;
    if (this.discovered === 0 || this.executed === 0) {
      process.stderr.write(
        '[app-e2e] refusing empty green run: ' +
          `discovered=${this.discovered}, executed=${this.executed}\n`,
      );
      return { status: 'failed' };
    }
    return undefined;
  }
}
