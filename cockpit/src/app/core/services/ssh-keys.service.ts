import {Injectable, inject} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {firstValueFrom} from 'rxjs';
import {environment} from '../environment';

/**
 * A registered SSH public key.
 *
 * Returned by `GET /api/ssh-keys` and `POST /api/ssh-keys`. Unlike a PAT,
 * the public key itself is never secret, so there is no reveal-once
 * banner here — the value worth protecting is the *private* half, which
 * never leaves the user's machine (see `SshKeyChallenge`).
 */
export interface SshKey {
  id: string;
  name: string;
  key_type: string;
  fingerprint: string;
  created_at: string;
  last_used_at: string | null;
  disabled: boolean;
}

/** A possession nonce from `POST /api/ssh-keys/challenge`. The caller must
 *  sign it with the private key before `createKey()` will accept it. */
export interface SshKeyChallenge {
  challenge: string;
  namespace: string;
  expires_at: string;
}

export interface CreateSshKeyRequest {
  name: string;
  publicKey: string;
  challenge: string;
  signature: string;
}

/**
 * Thin HTTP wrapper over `/api/ssh-keys*`. Deliberately holds no list state
 * of its own (unlike `api-keys.service.ts`'s signal-backed `keys`): every
 * method is Promise-returning (ruling P-4), and `SshKeysPageComponent` is
 * the sole owner of the presentation-layer list — one `GET` per mutation,
 * not two (fix round 1, minor "double fetch per mutation"). A second
 * consumer that wants a push-updated signal should add one deliberately,
 * not resurrect a copy that silently drifts from the component's.
 */
@Injectable({providedIn: 'root'})
export class SshKeysService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.apiUrl;

  /** The current user's SSH keys. Errors propagate — a failed request must
   *  never render as "no keys yet" (fix round 1, minor: a user who believes
   *  a listed key vanished may re-register it). */
  loadKeys(): Promise<SshKey[]> {
    return firstValueFrom(this.http.get<SshKey[]>(`${this.baseUrl}/ssh-keys`));
  }

  /** Issue a possession challenge. The caller signs `challenge` with the
   *  private key (namespace `namespace`) and pastes the result back into
   *  `createKey()`. */
  requestChallenge(): Promise<SshKeyChallenge> {
    return firstValueFrom(
      this.http.post<SshKeyChallenge>(`${this.baseUrl}/ssh-keys/challenge`, {}),
    );
  }

  /** Register a public key once possession is proven. */
  createKey(body: CreateSshKeyRequest): Promise<SshKey> {
    return firstValueFrom(
      this.http.post<SshKey>(`${this.baseUrl}/ssh-keys`, {
        name: body.name,
        public_key: body.publicKey,
        challenge: body.challenge,
        signature: body.signature,
      }),
    );
  }

  /** Remove a key. */
  async deleteKey(id: string): Promise<void> {
    await firstValueFrom(this.http.delete<{status: string}>(`${this.baseUrl}/ssh-keys/${id}`));
  }
}
