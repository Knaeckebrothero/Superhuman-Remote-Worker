import {APP_INITIALIZER, ApplicationConfig, isDevMode, provideBrowserGlobalErrorListeners} from '@angular/core';
import {provideRouter, withViewTransitions} from '@angular/router';
import {provideServiceWorker} from '@angular/service-worker';
import {HttpClient, provideHttpClient, withFetch, withInterceptors} from '@angular/common/http';
import {registerLocaleData} from '@angular/common';
import localeDe from '@angular/common/locales/de';
import {firstValueFrom} from 'rxjs';
import {authInterceptor} from './core/interceptors/auth.interceptor';
import {MARKED_EXTENSIONS, MARKED_OPTIONS, provideMarkdown} from 'ngx-markdown';
import {citationExtension} from './core/markdown/citation-extension';
import {SessionService} from './core/services/session.service';
import {UserService} from './core/services/user.service';
import {SettingsService} from './core/services/settings.service';
import {I18nService, SUPPORTED_LANGS, DEFAULT_LANG} from './core/services/i18n.service';
import {TranslocoHttpLoader} from './core/services/transloco-loader';
import {provideTransloco} from '@jsverse/transloco';
import {provideTranslocoLocale} from '@jsverse/transloco-locale';
import {User} from './core/models/api.model';
import {environment} from './core/environment';

import {routes} from './app.routes';
import {provideClientHydration, withEventReplay} from '@angular/platform-browser';

registerLocaleData(localeDe, 'de-DE');

/**
 * Bootstrap auth via the cookie BFF.
 *
 * GET /auth/me — if the browser already has a valid `srw_session` cookie,
 * the orchestrator returns the user payload. We pre-populate UserService
 * before any component renders, so guards can decide synchronously and
 * components never see a null-then-populated flash.
 *
 * On 401 (no cookie or expired), we redirect to /auth/login on the API
 * origin; the orchestrator generates PKCE state and bounces on to
 * Keycloak. After successful auth the user lands back on the original
 * route via /auth/callback's `return_to` redirect.
 */
function authBootstrap(
  http: HttpClient,
  session: SessionService,
  userService: UserService,
  i18n: I18nService,
  settings: SettingsService,
): () => Promise<void> {
  return async () => {
    i18n.applyInitialLanguage();
    try {
      const resp = await firstValueFrom(
        http.get<{ user: User }>(`${environment.apiUrl}/auth/me`),
      );
      if (resp?.user) {
        userService.currentUser.set(resp.user);
        session.authenticated.set(true);
        userService.loadUsers();
        settings.loadPreferences();
      }
    } catch {
      // 401 — interceptor already kicked off the BFF login redirect. Swallow
      // so app bootstrap resolves (the page is about to unload anyway).
    }
  };
}

export const appConfig: ApplicationConfig = {
  providers: [
    provideBrowserGlobalErrorListeners(),
    provideRouter(routes, withViewTransitions()),
    provideClientHydration(withEventReplay()),
    provideHttpClient(withFetch(), withInterceptors([authInterceptor])),
    provideTransloco({
      config: {
        availableLangs: [...SUPPORTED_LANGS],
        defaultLang: DEFAULT_LANG,
        fallbackLang: DEFAULT_LANG,
        reRenderOnLangChange: true,
        prodMode: !isDevMode(),
      },
      loader: TranslocoHttpLoader,
    }),
    provideTranslocoLocale({
      langToLocaleMapping: {
        en: 'en-US',
        'de-DE': 'de-DE',
      },
    }),
    {
      provide: APP_INITIALIZER,
      useFactory: authBootstrap,
      deps: [HttpClient, SessionService, UserService, I18nService, SettingsService],
      multi: true,
    },
    provideMarkdown({
      markedOptions: {
        provide: MARKED_OPTIONS,
        useValue: {
          gfm: true,
          breaks: true,
        },
      },
      markedExtensions: [
        {
          provide: MARKED_EXTENSIONS,
          multi: true,
          useValue: citationExtension(),
        },
      ],
    }),
    provideServiceWorker('ngsw-worker.js', {
      // Only register in production builds. Dev mode reloads frequently and
      // a stale SW would shadow code changes.
      enabled: !isDevMode(),
      // Defer registration until the app stabilises so SW install doesn't
      // contend with hydration / first-paint work.
      registrationStrategy: 'registerWhenStable:30000',
    }),
  ]
};
