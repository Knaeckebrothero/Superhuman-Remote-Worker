import {describe, expect, it} from 'vitest';
import {buildJetBrainsCommand, buildSshConfig} from './ssh-config';

describe('buildSshConfig', () => {
    const valid = {
        handle: 's-7f3a91c2',
        apiHost: 'api.srw.works',
        sshHost: 'ssh.srw.works',
        origin: 'https://cockpit.srw.works',
    };

    it('emits the documented directives', () => {
        const config = buildSshConfig(valid);
        expect(config).toContain('Host srw-s-7f3a91c2');
        expect(config).toContain('HostName            ssh.srw.works');
        expect(config).toContain('User                s-7f3a91c2');
        expect(config).toContain('ProxyCommand        srw-ssh-proxy --stdio api.srw.works');
    });

    it('sets keepalives, without which idle sessions die behind the CDN', () => {
        const config = buildSshConfig(valid);
        expect(config).toContain('ServerAliveInterval 30');
        expect(config).toContain('ServerAliveCountMax 3');
    });

    it('pins identities so the client does not offer every agent key', () => {
        expect(buildSshConfig(valid)).toContain('IdentitiesOnly      yes');
    });

    it('carries the calling origin so the gateway Origin allow-list matches (ruling P-8)', () => {
        expect(buildSshConfig(valid)).toContain(
            'ProxyCommand        srw-ssh-proxy --stdio api.srw.works --origin https://cockpit.srw.works',
        );
    });

    // Each of these would place attacker-chosen directives in ~/.ssh/config.
    const hostile = [
        's-7f3a91c2\nProxyCommand rm -rf ~',
        's-7f3a91c2 ProxyCommand evil',
        's-7f3a91c2\r\nUser root',
        's-ABCDEFGH',
        's-short',
        '',
        'refactor-auth',
    ];
    hostile.forEach((handle) => {
        it(`refuses to emit a block for ${JSON.stringify(handle)}`, () => {
            expect(() => buildSshConfig({...valid, handle})).toThrow();
        });
    });

    it('refuses a hostile hostname too', () => {
        expect(() =>
            buildSshConfig({...valid, sshHost: 'ssh.srw.works\nProxyCommand evil'}),
        ).toThrow();
    });

    // The origin is interpolated into ~/.ssh/config exactly like handle and
    // sshHost, so it is exactly as much of an injection sink as they are.
    // Ruling P-10 widened the accepted grammar to http(s) + optional port
    // (see below), so a scheme or a port alone is no longer hostile — only
    // shapes that are actually dangerous in a shell/config-file context stay
    // in this list: newline/CR, embedded whitespace, path, query, a bare
    // host with no scheme, empty string, a tab, a NUL byte, and userinfo.
    const hostileOrigins = [
        'https://cockpit.srw.works\nProxyCommand rm -rf ~',
        'https://cockpit.srw.works ProxyCommand evil',
        'https://cockpit.srw.works\r\nUser root',
        'https://cockpit.srw.works/path',
        'https://cockpit.srw.works?query=1',
        'cockpit.srw.works',
        '',
        'https://cockpit.srw.works\tProxyCommand evil',
        'https://cockpit.srw.works\u0000',
        'https://user@cockpit.srw.works',
    ];
    hostileOrigins.forEach((origin) => {
        it(`refuses a hostile origin ${JSON.stringify(origin)}`, () => {
            expect(() => buildSshConfig({...valid, origin})).toThrow();
        });
    });

    // Regression tests for ruling P-10: a k3d port-forwarded cockpit serves
    // http://localhost:PORT, and Task 5 feeds this function the real
    // window.location.origin, so both of these must be accepted and carried
    // through intact rather than rejected. (These two values previously sat
    // in hostileOrigins above; the review caught that the pre-P-10 grammar
    // rejected them only for the port and the scheme respectively, not for
    // anything actually dangerous.)
    it('accepts an origin with a port, since ruling P-10 allows it', () => {
        const config = buildSshConfig({...valid, origin: 'https://cockpit.srw.works:4200'});
        expect(config).toContain(
            'ProxyCommand        srw-ssh-proxy --stdio api.srw.works --origin https://cockpit.srw.works:4200',
        );
    });

    it('accepts an http origin, since ruling P-10 allows it (k3d port-forwarded cockpit)', () => {
        const config = buildSshConfig({...valid, origin: 'http://cockpit.srw.works'});
        expect(config).toContain(
            'ProxyCommand        srw-ssh-proxy --stdio api.srw.works --origin http://cockpit.srw.works',
        );
    });

    it('never interpolates a free-text title', () => {
        // The Host alias is derived from the handle alone, never from thread.title.
        expect(buildSshConfig(valid)).not.toContain('refactor');
    });
});

describe('buildJetBrainsCommand', () => {
    it('uses listener mode, since Gateway cannot use ProxyCommand', () => {
        expect(buildJetBrainsCommand({apiHost: 'api.srw.works'})).toBe(
            'srw-ssh-proxy --listen 127.0.0.1:2222 api.srw.works',
        );
    });

    it('refuses a hostile apiHost too', () => {
        expect(() =>
            buildJetBrainsCommand({apiHost: 'api.srw.works\nProxyCommand evil'}),
        ).toThrow();
    });
});
