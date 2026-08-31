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
    const hostileOrigins = [
        'https://cockpit.srw.works\nProxyCommand rm -rf ~',
        'https://cockpit.srw.works ProxyCommand evil',
        'https://cockpit.srw.works\r\nUser root',
        'https://cockpit.srw.works/path',
        'https://cockpit.srw.works?query=1',
        'https://cockpit.srw.works:4200',
        'http://cockpit.srw.works',
        'cockpit.srw.works',
        '',
    ];
    hostileOrigins.forEach((origin) => {
        it(`refuses a hostile origin ${JSON.stringify(origin)}`, () => {
            expect(() => buildSshConfig({...valid, origin})).toThrow();
        });
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
