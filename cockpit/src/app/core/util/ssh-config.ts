/**
 * Generates the ~/.ssh/config block shown on the session connect panel.
 *
 * Generated SSH config is an injection sink: a newline in an interpolated value
 * places attacker-chosen directives — ProxyCommand among them — into the user's
 * own config. Only the CSPRNG handle, the server-supplied hostnames, and the
 * calling page's own origin are ever interpolated, and all of them are
 * validated here as well as at their own source of truth (the handle is
 * re-validated at mint by services/ssh_handles.py; the origin has to match
 * the gateway's exact-match Origin allow-list — helm/values.yaml
 * `allowedOrigins`, no default in either direction, ruling P-8 — or the
 * connection is refused there regardless of what this function accepts).
 */

/** Crockford base32 minus the ambiguous i/l/o/u. Mirrors services/ssh_handles.py.
 *  Anchored with both `^` and `$` and deliberately NO `m` flag: unlike
 *  Python's `re`, JavaScript's `$` (without `m`) matches only the absolute
 *  end of the string, not just before a trailing newline. That is what makes
 *  this reject "s-7f3a91c2\nProxyCommand ..." instead of matching the first
 *  line and silently accepting the rest. Do not add the `m` flag. */
export const HANDLE_PATTERN = /^s-[0-9abcdefghjkmnpqrstvwxyz]{8}$/;

/** Bare hostname/host-label grammar, shared by HOST_PATTERN and ORIGIN_PATTERN
 *  so the two can't drift apart the way a hand-copied second charset could. */
const HOST_BODY = '[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*';

/** Hostnames only: letters, digits, dots and hyphens. No `m` flag, same reasoning as above. */
const HOST_PATTERN = new RegExp(`^${HOST_BODY}$`, 'i');

/**
 * `window.location.origin` and nothing else — scheme plus host, no port, no
 * path, no query, no whitespace, no newline. Deployed cockpit is always
 * https, so that's the only scheme accepted; anything this rejects that a
 * real deployment legitimately needs (e.g. a non-standard port) is a dead
 * end that surfaces as a thrown Error, not a security hole. The reverse —
 * this function accepting a shape the gateway's Origin allow-list would not
 * exact-match — is the actual sink being guarded against.
 */
const ORIGIN_PATTERN = new RegExp(`^https://${HOST_BODY}$`, 'i');

export interface SshConfigOptions {
    handle: string;
    apiHost: string;
    sshHost: string;
    origin: string;
}

function assertSafe(handle: string, apiHost: string, sshHost: string, origin: string): void {
    if (!HANDLE_PATTERN.test(handle)) {
        throw new Error('Refusing to generate SSH config for an invalid session handle.');
    }
    for (const host of [apiHost, sshHost]) {
        if (!HOST_PATTERN.test(host)) {
            throw new Error('Refusing to generate SSH config for an invalid hostname.');
        }
    }
    if (!ORIGIN_PATTERN.test(origin)) {
        throw new Error('Refusing to generate SSH config for an invalid origin.');
    }
}

export function buildSshConfig({handle, apiHost, sshHost, origin}: SshConfigOptions): string {
    assertSafe(handle, apiHost, sshHost, origin);
    // HostName is never dialled — ProxyCommand short-circuits DNS — but it is the
    // known_hosts key, so the stable gateway host key lands under one name.
    return [
        `Host srw-${handle}`,
        `    HostName            ${sshHost}`,
        `    User                ${handle}`,
        `    ProxyCommand        srw-ssh-proxy --stdio ${apiHost} --origin ${origin}`,
        `    IdentityFile        ~/.ssh/id_ed25519`,
        `    IdentitiesOnly      yes`,
        `    PreferredAuthentications publickey`,
        `    ServerAliveInterval 30`,
        `    ServerAliveCountMax 3`,
        `    ControlMaster       auto`,
        `    ControlPath         ~/.ssh/srw-%C`,
        `    ControlPersist      10m`,
    ].join('\n');
}

export function buildJetBrainsCommand({apiHost}: {apiHost: string}): string {
    if (!HOST_PATTERN.test(apiHost)) {
        throw new Error('Refusing to generate a command for an invalid hostname.');
    }
    // Gateway ignores IdentityFile, User, Port and ControlMaster, and has no
    // "use system OpenSSH" option — so it gets a local listener, not a config block.
    return `srw-ssh-proxy --listen 127.0.0.1:2222 ${apiHost}`;
}
