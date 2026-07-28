---
tags:
  - issue
  - orchestrator
  - vm
  - provisioning
  - templating
---

# A job description containing a newline (or a quote) destroys the VM manifest — `${DESCRIPTION}` is substituted raw into a JSON blob nested inside cloud-init's `userData:` block scalar

**Status:** FIXED 2026-07-26, deployed to dev 2026-07-27, **live-verified
2026-07-28** — job `4435994d` provisioned a real VM
(`context.vm = {"status": "ready", "vm_name": "agent-vm-4435994d…"}`) and ran to
`reviewing` with 181 audit entries, while its stored description **still carried
the trailing blank line** that originally broke it (nothing retro-strips
existing rows). The deployed renderer therefore handled the exact byte sequence
that failed on 07-26.
**Severity:** high — any job whose description contains a newline could never
get a VM, and the failure looked random per-job because sibling jobs with clean
one-line descriptions provisioned fine.
**Component:** `helm/templates/vm-controller/configmap.yaml:88` and
`helm-vm-cluster/templates/vm-controller/configmap.yaml` (both vulnerable);
`orchestrator/services/vm_provisioner.py` `_render_template`;
`vm/controller/controller.py` `render_template`.
**Motivating incident:** job `4435994d-b029-444d-8a3c-26c64abd456a` ("Job 1C —
Contemporary Hotel Neutral"), dev cluster, 2026-07-26. Failed with **0 audit
entries, 0 tokens, no log file** — it died before any pod existed:

```
VM provisioning failed: while scanning a simple key
  in "<unicode string>", line 80, column 1:
    ",
    ^
could not find expected ':'
  in "<unicode string>", line 81, column 24:
                          "nats_url": "nats://srw-vm-nats-l ...
```

Its description was `"Job 1C — Contemporary Hotel Neutral\n\n"`. Sibling jobs
1A and 1B, same batch and config, differed only in having no trailing blank
line and provisioned normally.

## Root cause

The VM template embeds a JSON blob inside a **YAML block scalar**:

```yaml
              cloudInitNoCloud:
                userData: |
                  write_files:
                    - path: /run/agent/job-config.json
                      content: |
                        {
                          "description": "${DESCRIPTION}",
                          "nats_url": "${NATS_URL}"
                        }
```

`${DESCRIPTION}` was replaced by raw string substitution. A newline in the value
puts the continuation at **column 1**, which dedents out of the block scalar and
terminates `userData`; YAML then parses the description's tail as outer mapping
keys, hits the orphaned `",` and fails on the following line. The reported line
numbers refer to the *rendered* template, not to any file on disk — which is why
they match nothing when you go looking.

The line numbers also identify **which** component rendered: 80/81 is the
orchestrator's `helm/` template. The vm-controller's copy would have reported
81/82 (it carries two extra `ORCHESTRATOR_ID` lines).

## Symptom shapes (all one root cause)

| Description contains | Result |
|---|---|
| trailing newline(s) | YAML fails at provision — loud, fast, diagnosable |
| **internal** newlines | YAML fails at provision — survives a trailing-whitespace strip |
| a `"` or a `\` | **YAML parses fine.** Manifest applies, VM boots, `job-config.json` is corrupt — `management-daemon.py` swallows it with a bare `log.warning` and falls back to env-file defaults, silently losing `agent_config`/`vm_image`/`cpu_cores`/`memory` |
| indented YAML | Manifest passes `yaml.safe_load` and KubeVirt admission while its `userData` is malformed — a VM that boots with none of its configuration |
| >~350 chars | Blows the hard **2048-byte** KubeVirt inline `userData` limit — headroom was only 350–450 bytes |

The quote/backslash rows matter most: they fail *silently*, so the loud YAML
crash was the good outcome by comparison.

## Fix shipped

`_escape_for_job_config()` = `json.dumps(value)[1:-1]` (drops the quotes the
template already supplies), applied at **both** render sites. They live in
separate images with no shared imports, so the duplication is structural — both
copies carry a keep-in-sync note.

Length is capped at `MAX_DESCRIPTION_LEN = 200` to protect the 2048-byte limit.
Safe because `job-config.json` has exactly one consumer
(`docker/agent-vm-base/files/management-daemon.py`) and it **never reads
`description`** — the field is write-only.

`orchestrator/database/postgres.py::create_job` also `.strip()`s the
description. That is defence in depth only, explicitly **not** the fix: it does
nothing for internal newlines, quotes, or backslashes.

Sanitising at the six creation entry points was rejected in favour of escaping
at the two render sites — the renderer is the only code that knows the target
grammar is JSON-inside-a-YAML-block-scalar, and callers have no business
knowing a downstream serializer is fragile.

## Why the tests did not catch this

`tests/test_vm_provisioner.py` and `tests/test_vm_controller.py` both already
had a passing `..._special_characters_in_description` test. Both render
*fabricated* stand-in templates that put the description in a plain flow scalar
with no block scalar and no JSON blob — so the defect could not exist in them.

`tests/test_vm_template_description_escaping.py` therefore renders the **real
chart templates**, extracting the block scalar out of the ConfigMap. Lesson: a
fixture that simplifies the artifact under test cannot catch structural defects
in that artifact.

## Notes

- "Clear `context.vm` and re-queue" (the park error's own advice) does **not**
  help on its own — the same description re-renders the same broken YAML. Deploy
  the fix or edit the description.
- Em dashes and other Unicode are harmless: `json.dumps` ensure_ascii turns them
  into `\uXXXX`, which round-trips.

## Related

- `docs/done/vm_controller_headscale_latch_kills_provisioning.md` — the *other*
  VM provisioning failure mode ("provisioning exhausted after 3 attempts").
- `docs/done/resume_never_provisions_a_missing_workspace.md` — hit while trying
  to recover this very job; the Resume button could not bring it back.
