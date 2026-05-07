import {Injectable} from '@angular/core';
import {Subject} from 'rxjs';

/**
 * Cross-tab coordination for the admin LLM page. The Providers tab can
 * request that the Models tab open with a specific endpoint pre-selected
 * and discovery auto-fired — useful for "Discover models" actions on
 * endpoint cards.
 */
@Injectable({providedIn: 'root'})
export class AdminLlmCoordinatorService {
  readonly discoverEndpoint$ = new Subject<string>();
}
