import {describe, expect, it} from 'vitest';
import {HttpErrorResponse} from '@angular/common/http';
import {Injector, runInInjectionContext} from '@angular/core';
import {TranslocoService} from '@jsverse/transloco';
import {ErrorMessageService} from './error-message.service';

/** Transloco stub with a small catalog; unknown keys echo back, which is how
 *  the real service detects "no translation for this key". */
const CATALOG: Record<string, string> = {
  'errors.http.4xx': "The request couldn't be completed.",
  'errors.http.5xx': 'Something went wrong on our end.',
  'errors.code.job.not_found': 'That job no longer exists.',
  'errors.unknown': 'Unknown error.',
  'errors.jobs.createFailed': 'The job could not be created.',
};

function makeService(): ErrorMessageService {
  const injector = Injector.create({
    providers: [
      {
        provide: TranslocoService,
        useValue: {translate: (key: string) => CATALOG[key] ?? key},
      },
    ],
  });
  return runInInjectionContext(injector, () => new ErrorMessageService());
}

const httpError = (status: number, body: unknown) =>
  new HttpErrorResponse({status, error: body});

describe('ErrorMessageService', () => {
  // The regression: `errors.http.4xx` is defined for every 4xx, so it always
  // won and the server's explanation underneath was unreachable. On a form the
  // user is meant to correct, that is the difference between an actionable
  // message and a shrug.
  it('prefers a 4xx detail over the generic per-status message', () => {
    const svc = makeService();
    const detail =
      'Lite session backends cannot attach clone-based repository connectors';

    expect(svc.translate(httpError(400, {detail}))).toBe(detail);
  });

  it('still prefers a structured code over the raw detail', () => {
    const svc = makeService();

    expect(
      svc.translate(httpError(404, {code: 'job.not_found', detail: 'no row'})),
    ).toBe('That job no longer exists.');
  });

  // `create_thread`'s catch-all raises HTTPException(500, detail=str(e)), so a
  // 500 detail is raw internal exception text, not an instruction.
  it('keeps the generic message for 500, whose detail is internal', () => {
    const svc = makeService();

    expect(
      svc.translate(httpError(500, {detail: 'psycopg.errors.UndefinedColumn'})),
    ).toBe('Something went wrong on our end.');
  });

  // 503 is this API's deliberate "this deployment can't do that" answer — the
  // readiness gate and "VM provisioning is not available on this deployment".
  // Caught live on k3d: the VM tier was rejected and the form said only
  // "The server encountered an error."
  it('uses a structured 503 detail, which is a deliberate refusal', () => {
    const svc = makeService();
    const detail = 'VM provisioning is not available on this deployment';

    expect(svc.translate(httpError(503, {detail}))).toBe(detail);
  });

  // A gateway serving an HTML error page arrives as a string body; rendering it
  // would put markup in the message box.
  it('ignores an unstructured body and stays generic', () => {
    const svc = makeService();

    expect(
      svc.translate(httpError(503, '<html><body>503 Service Unavailable</body></html>')),
    ).toBe('Something went wrong on our end.');
  });

  it('falls back to the generic 4xx line when the server sent no detail', () => {
    const svc = makeService();

    expect(svc.translate(httpError(400, {}))).toBe(
      "The request couldn't be completed.",
    );
  });

  it('uses the caller fallback when there is neither code nor status match', () => {
    const svc = makeService();

    expect(svc.translate({}, 'errors.jobs.createFailed')).toBe(
      'The job could not be created.',
    );
  });
});
