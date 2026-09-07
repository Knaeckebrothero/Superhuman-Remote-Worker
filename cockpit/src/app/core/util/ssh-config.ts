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
 *
 * There's a second reason the charsets below are narrow: `ProxyCommand` is
 * executed through `/bin/sh -c`, so every interpolated value lands in a
 * *shell* context, not merely a config-file context. The grammars here admit
 * only alphanumerics, dot, hyphen, colon and the scheme's `//` — none of
 * which are shell metacharacters — which is why a validated value is safe to
 * hand to `sh -c` as well as to the config parser. If this is ever loosened
 * toward "general URL characters" (spaces, `?`, `#`, `@`, quotes, `$`, `` ` ``,
 * `;`, `&`, `|`, ...), config injection becomes shell injection. Widen with
 * that in mind, not just with the config-file sink in mind.
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
 * `window.location.origin` and nothing else — scheme, host, and an optional
 * port, no userinfo, no path, no query, no fragment, no whitespace, no
 * control characters.
 *
 * Both `http` and `https` are accepted, and a port (1-5 digits) is allowed:
 * ruling P-10. A k3d port-forwarded cockpit serves `http://localhost:PORT`,
 * and Task 5 feeds this function the real `window.location.origin` — so a
 * scheme- or port-only restriction would refuse to render the connect panel
 * in exactly the environment Task 7's live gate runs in. Neither the scheme
 * nor a numeric port is a shell metacharacter, so widening to admit them
 * costs nothing on the injection side (see the module comment above); the
 * only cost of accepting `http` is a 403 from a gateway whose Origin
 * allow-list happens to list only `https` origins, which is a normal,
 * already-named failure (Task 1's error message), not a security hole.
 * The rule is: fail closed on the *dangerous* grammar, not the *unfamiliar*
 * one. Do not re-tighten this back to `https`-only or portless.
 */
const ORIGIN_PATTERN = new RegExp(`^https?://${HOST_BODY}(:\\d{1,5})?$`, 'i');

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

export function buildJetBrainsCommand({apiHost, origin}: {apiHost: string; origin: string}): string {
    if (!HOST_PATTERN.test(apiHost)) {
        throw new Error('Refusing to generate a command for an invalid hostname.');
    }
    if (!ORIGIN_PATTERN.test(origin)) {
        throw new Error('Refusing to generate a command for an invalid origin.');
    }
    // Gateway ignores IdentityFile, User, Port and ControlMaster, and has no
    // "use system OpenSSH" option — so it gets a local listener, not a config block.
    //
    // `--origin` is required here for the same reason buildSshConfig always
    // sends it (ruling P-8): srw-ssh-proxy's own fallback guess
    // (api.<domain> -> https://cockpit.<domain>) is wrong on this chart's own
    // default topology, where cockpit serves the apex domain rather than a
    // cockpit.<domain> subdomain (helm/values.yaml `global.hostnames.cockpit`
    // defaults to "" -> the bare domain). A wrong guess is a bare 403 at the
    // WebSocket upgrade — the one client here that cannot fall back to the
    // config block above (ruling I-2).
    return `srw-ssh-proxy --listen 127.0.0.1:2222 ${apiHost} --origin ${origin}`;
}
