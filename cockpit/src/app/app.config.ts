import {APP_INITIALIZER, ApplicationConfig, isDevMode, provideBrowserGlobalErrorListeners} from '@angular/core';
import {provideRouter, withViewTransitions} from '@angular/router';
import {provideServiceWorker} from '@angular/service-worker';
import {provideHttpClient, withFetch, withInterceptors} from '@angular/common/http';
import {registerLocaleData} from '@angular/common';
import localeDe from '@angular/common/locales/de';
import {authInterceptor} from './core/interceptors/auth.interceptor';
import {MARKED_EXTENSIONS, MARKED_OPTIONS, provideMarkdown} from 'ngx-markdown';
import {citationExtension} from './core/markdown/citation-extension';
import {KeycloakService} from './core/services/keycloak.service';
import {SettingsService} from './core/services/settings.service';
import {I18nService, SUPPORTED_LANGS, DEFAULT_LANG} from './core/services/i18n.service';
import {TranslocoHttpLoader} from './core/services/transloco-loader';
import {provideTransloco} from '@jsverse/transloco';
import {provideTranslocoLocale} from '@jsverse/transloco-locale';

import {routes} from './app.routes';
import {provideClientHydration, withEventReplay} from '@angular/platform-browser';

registerLocaleData(localeDe, 'de-DE');

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
      useFactory: (kc: KeycloakService, i18n: I18nService, settings: SettingsService) => () =>
        kc.init().then(() => {
          i18n.applyInitialLanguage();
          if (kc.authenticated) {
            settings.loadPreferences();
          }
        }),
      deps: [KeycloakService, I18nService, SettingsService],
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
