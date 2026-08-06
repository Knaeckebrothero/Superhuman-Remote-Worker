# k3d: workspace pods reject the provisioner's SSH key (all jobs fail pre-dispatch)

**Status:** RESOLVED — root-caused + live-verified 2026-08-06 (batch fix
session). Not key drift at all: the Secret was internally consistent
(`srw-vm-ssh-key` private key == offered fingerprint `SHA256:y7t/35gU/…` ==
the pod's installed `authorized_keys`). The real chain: `4f27f581`
(08-03 12:21) ADDED the SSH-auth readiness gate (`wait_for_agent_ssh`) to
`_wait_for_ready` — before it the provisioner returned pod IP on K8s-ready
without probing auth. The probe then failed because the chart projects the
Secret key as **0444** and root-running OpenSSH refuses a group/other-readable
identity file ("bad permissions → Permission denied (publickey)") — the doc's
quoted error tail was just ssh's known-hosts warning line, truncated by log
formatting. Timeline fits exactly: last success 08-02 18:40, gate landed
08-03 12:21, first failure 08-03 14:47. The FIX also already landed:
`52c1ba80` (08-04 22:36) added `_stage_runtime_ssh_key`
(`resolve_ssh_key_path` stages an atomic runtime-owned 0600 copy) + its
tests — hours AFTER this doc was filed, and nobody re-ran a job.
Live verification 2026-08-06: manual probe from the orchestrator pod with the
staged key → exit 0; smoke job `58ba61ef` (plain POST /api/jobs) provisioned
("Workspace SSH authenticated … attempts=1"), reached `processing`, made LLM
calls, and **completed**; three later jobs provisioned the same way
(1-attempt SSH auth each). Dev was never affected because its non-root
production image passes OpenSSH's other-uid special case.
Residual papercut kept open in this doc's Impact section: a provisioning
failure writes only `failed` + empty `error` column (reason lives solely in
orchestrator logs).
**Originally:** Open, environment-only (k3d cluster `srw`). Dev cluster unaffected
(jobs provisioned fine there the same day). Filed 2026-08-04 during the Job
Bench k3d smoke.

## Symptom

Every job created on the local k3d cluster fails before its first LLM call:

```
services.container_provisioner: Failed to create workspace container for job <id>:
Workspace pod became Kubernetes-ready but rejected the configured SSH key after
15 attempt(s) (key=SHA256:y7t/35gU/pkHno3j0szof/bD5jKqPk0pWx7P3+nhuc4): (ED25519)
to the list of known hosts.
```

The PVC, Service, and pod all create fine; sshd answers; the key is refused.
The job row ends `failed` with an empty `error` column (secondary papercut:
the provisioning failure reason is only in orchestrator logs, not on the job).

## Evidence it is environmental, not code

- Reproduced 2026-08-04 with a **plain `POST /api/jobs` control job**
  (`799c2a55`, no bench involvement) — identical failure to the bench smoke
  job (`4767ce8b`).
- Timeline from the k3d jobs table: last completed job `93366372`
  2026-08-02 18:40; first failure `67e4e76b` 2026-08-03 14:47 — the breakage
  window predates any Job Bench code reaching the cluster (2026-08-04 09:53).
- The same working tree's orchestrator code provisions workspaces fine on the
  dev cluster; `container_provisioner.py` has no local modifications.

## Suspects (unverified)

Something in the 08-02→08-03 window on this cluster: workspace image drift vs.
the SSH-credential Secret (key rotation without pod/image alignment), a Helm
values change, or a parallel session's cluster surgery. The fingerprint in the
error is the key the *provisioner* is offering — compare it against the
authorized key baked/mounted into the workspace pod
(`kubectl -n srw exec workspace-<id> -- cat ~/.ssh/authorized_keys`).

## Impact

- No job can execute end-to-end on k3d until fixed → job-path smokes
  (including Job Bench §5 full-metrics validation) are blocked locally and
  must run on dev post-merge.
- Session workspaces may be affected too (untested in this session).

## Next debugging step

Compare the provisioner's offered public key (Secret it reads) with the
workspace pod's `authorized_keys`; whichever side changed on 08-03 is the
culprit. Then decide: re-mint the Secret or rebuild/pin the workspace image.
