# Sudo approval timeouts

A pending sudo command has a 30-minute approval window by default. Three
independent settings must agree:

| Layer | Setting | Default |
| --- | --- | --- |
| Orchestrator database expiry | `SUDO_COMMAND_TTL_SECONDS` environment variable | `1800` seconds |
| Daemon decision context, for HTTP and legacy NATS | `timeouts.nats_request` in `/etc/sudo-gate/config.yaml` | `1830s` |
| C plugin response wait | `timeout` option on the `Plugin sudo_gate_approval` line in `/etc/sudo.conf` | `1845` seconds |

The daemon reserves 30 seconds beyond the approval window for request creation
and delivery. The plugin adds 15 seconds beyond the daemon, covering the
10-second socket read budget and five seconds for the response. Transport still
fails closed if its deadline expires. HTTP's 35-second attempt timeout and
25-second long polls are individual attempts inside the total decision budget;
they do not shorten the human approval window. Legacy NATS uses the same total
context in `RequestWithContext`; unauthenticated guest NATS remains disabled.

For a custom approval window of `T` seconds, configure the orchestrator with `T`,
the daemon with `T + 30` seconds, and the plugin with `T + 45` seconds. These are
separate process configurations; changing the orchestrator environment alone
does not update a VM. The plugin accepts at most 3600 seconds, so coordinated
windows must be no greater than 3555 seconds. A shorter orchestrator TTL still
expires and denies the request before the transport budget is exhausted.
`vm_upgrade` requests retain their separate 24-hour database TTL.

The base-image files and compiled defaults are aligned for new VMs. Rebuild and
roll out the VM image, or update both configuration files and restart
`sudo-gated` on existing VMs, before relying on the longer window there.
