import {Injectable, inject, signal} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {catchError, firstValueFrom, of} from 'rxjs';
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

@Injectable({providedIn: 'root'})
export class SshKeysService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = environment.apiUrl;

  /** All known SSH keys for the current user. */
  readonly keys = signal<SshKey[]>([]);
  readonly isLoading = signal(false);

  /** Load the current user's SSH keys into the signal. */
  async loadKeys(): Promise<SshKey[]> {
    this.isLoading.set(true);
    try {
      const keys = await firstValueFrom(
        this.http
          .get<SshKey[]>(`${this.baseUrl}/ssh-keys`)
          .pipe(catchError(() => of([] as SshKey[]))),
      );
      this.keys.set(keys);
      return keys;
    } finally {
      this.isLoading.set(false);
    }
  }

  /** Issue a possession challenge. The caller signs `challenge` with the
   *  private key (namespace `namespace`) and pastes the result back into
   *  `createKey()`. */
  requestChallenge(): Promise<SshKeyChallenge> {
    return firstValueFrom(
      this.http.post<SshKeyChallenge>(`${this.baseUrl}/ssh-keys/challenge`, {}),
    );
  }

  /** Register a public key once possession is proven. Refreshes the list
   *  on success. */
  async createKey(body: CreateSshKeyRequest): Promise<SshKey> {
    const created = await firstValueFrom(
      this.http.post<SshKey>(`${this.baseUrl}/ssh-keys`, {
        name: body.name,
        public_key: body.publicKey,
        challenge: body.challenge,
        signature: body.signature,
      }),
    );
    await this.loadKeys();
    return created;
  }

  /** Remove a key. Refreshes the list on success. */
  async deleteKey(id: string): Promise<void> {
    await firstValueFrom(this.http.delete<{status: string}>(`${this.baseUrl}/ssh-keys/${id}`));
    await this.loadKeys();
  }
}
