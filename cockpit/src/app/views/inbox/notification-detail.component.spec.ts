import {describe, expect, it} from 'vitest';

import {sudoCommandLine} from './notification-detail.component';
import {SudoRequestRow} from '../../core/models/notification.model';

/**
 * The sudo card must show the command line the gate evaluated.
 *
 * `command` alone is the resolved binary: for
 * `sudo -u agent-host bash --login -c '…'` it is `/bin/bash`, so an approver
 * reading the old `arguments?.join(' ') || command` binding judged "a bare
 * shell" instead of the script. This mirrors the orchestrator's
 * `shared/sudo_command_line.py`, so both surfaces render one identical line.
 */

function sudoRow(overrides: Partial<SudoRequestRow> = {}): SudoRequestRow {
  return {
    id: 'f678a94d-2f1e-4f0a-9a1c-7b3d5e2c8a10',
    job_id: null,
    thread_id: null,
    vm_name: 'agent-vm-397b',
    command: '/bin/bash',
    arguments: ['bash', '--login', '-c', 'id && docker version'],
    working_directory: '/workspace',
    requesting_user: 'agent-host',
    target_user: 'agent-host',
    status: 'pending',
    requested_at: '2026-09-05T18:12:00+00:00',
    expires_at: '2026-09-05T18:42:00+00:00',
    request_type: 'sudo_command',
    metadata: null,
    ...overrides,
  };
}

describe('sudoCommandLine', () => {
  it('shows the script a login-shell wrapper runs, not the bare shell', () => {
    expect(sudoCommandLine(sudoRow())).toBe(`/bin/bash --login -c 'id && docker version'`);
  });

  it('quotes arguments that need it', () => {
    const line = sudoCommandLine(
      sudoRow({command: '/usr/bin/tee', arguments: ['tee', '/etc/my file.conf']}),
    );
    expect(line).toBe(`/usr/bin/tee '/etc/my file.conf'`);
  });

  it('keeps argv[0] when it differs from the binary', () => {
    const line = sudoCommandLine(
      sudoRow({command: '/bin/busybox', arguments: ['sh', '-c', 'id']}),
    );
    expect(line).toBe('/bin/busybox sh -c id');
  });

  it('renders the command alone when there are no arguments', () => {
    expect(sudoCommandLine(sudoRow({command: '/usr/bin/id', arguments: []}))).toBe('/usr/bin/id');
    expect(sudoCommandLine(sudoRow({command: '/usr/bin/id', arguments: null}))).toBe('/usr/bin/id');
  });

  it('escapes an embedded single quote', () => {
    const line = sudoCommandLine(
      sudoRow({command: '/bin/sh', arguments: ['sh', '-c', "echo 'hi'"]}),
    );
    expect(line).toBe(`/bin/sh -c 'echo '\\''hi'\\'''`);
  });

  it('falls back to the arguments when the command is missing', () => {
    expect(sudoCommandLine(sudoRow({command: '', arguments: ['ls', '-la']}))).toBe('ls -la');
  });
});
