import {APP_INITIALIZER, ApplicationConfig, provideBrowserGlobalErrorListeners} from '@angular/core';
import {provideRouter, withViewTransitions} from '@angular/router';
import {provideHttpClient, withFetch, withInterceptors} from '@angular/common/http';
import {authInterceptor} from './core/interceptors/auth.interceptor';
import {MARKED_EXTENSIONS, MARKED_OPTIONS, provideMarkdown} from 'ngx-markdown';
import {citationExtension} from './core/markdown/citation-extension';
import {KeycloakService} from './core/services/keycloak.service';

import {routes} from './app.routes';
import {provideClientHydration, withEventReplay} from '@angular/platform-browser';

export const appConfig: ApplicationConfig = {
  providers: [
    provideBrowserGlobalErrorListeners(),
    provideRouter(routes, withViewTransitions()),
    provideClientHydration(withEventReplay()),
    provideHttpClient(withFetch(), withInterceptors([authInterceptor])),
    {
      provide: APP_INITIALIZER,
      useFactory: (kc: KeycloakService) => () => kc.init(),
      deps: [KeycloakService],
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
  ]
};
