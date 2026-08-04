# k3d: workspace pods reject the provisioner's SSH key (all jobs fail pre-dispatch)

**Status:** Open, environment-only (k3d cluster `srw`). Dev cluster unaffected
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
