---
tags:
  - issue
  - security
  - docs
  - public-repo
status: open
priority: P1
created: 2026-08-16
aliases:
  - home IP in the public repo
  - docs vault leaks operator identifiers
related:
  - "[[srw_public_repo_ships_docs_vault]]"
  - "[[public_ip_exposure_policy]]"
---

# The public repo ships a workstation IP address and operator email addresses

**Status:** OPEN. Found 2026-08-16 while auditing an unrelated credential exposure.
The credential scare was a false alarm; this is what the scan actually found.

`Knaeckebrothero/Superhuman-Remote-Worker` is **PUBLIC**.

**This document deliberately does not restate the values.** They are identified by
the secret key that holds them and by the files that contain them.

## What is exposed

**1. A raw IPv4 address — the value of `VPN_WORKSTATION_ENDPOINT`.**

Present at `HEAD` in:

- `docs/issues/results.md`
- `docs/issues/gemma_tool_call_parser_loop.md`

and introduced or removed across four commits: `1ede9dbf`, `786d0199`, `dd29c929`,
`a77ab421`. It is therefore in history, not only in the working tree.

This is precisely what [[public_ip_exposure_policy]] exists to prevent — a home
IPv4 reachable without authentication and without a Cloudflare Tunnel in front of
it.

**2. Five operator email addresses** — the values of `IMAP_USER`, `SMTP_USER`,
`PROTON_USER`, `PGADMIN_EMAIL`, `UNPAYWALL_EMAIL` — in
`docker/keycloak/realm-export.json` and `docs/deployment_roadmap.md` (plus several
`docs/done/` postmortems). Identifiers rather than secrets, and one of them is
already semi-public, so this is materially lower severity than the IP.

## Checked and confirmed NOT exposed

All 57 values in the `srw` secret were tested against the working tree, `HEAD`, and
every commit on every ref. Clean:

- `POSTGRES_PASSWORD`, `AUDIT_POSTGRES_PASSWORD`, `GITEA_ADMIN_PASSWORD`,
  `GITEA_DB_PASSWORD` — never appear in any commit, any branch.
- Every `*_SECRET_ACCESS_KEY` / `*_SECRET_KEY`. Three S3 **access key IDs** do match
  committed compose and values files, but an access key ID without its secret is not
  a credential.

## Why it matters

This is [[srw_public_repo_ships_docs_vault]] paying out in a concrete way. The 819
design docs were written in the register of private engineering notes — real
hostnames, real addresses, real operational detail — and they are world-readable.
The IP got there through an ordinary debugging note, not through carelessness about
secrets, which is exactly why it is easy to repeat.

## Direction

For the IP, weigh the options honestly rather than defaulting to a purge:

- A history rewrite on a public repo with forks and existing clones is usually worse
  than the exposure it fixes, and does not un-publish anything already scraped.
- **Renumbering the endpoint** retires the value outright and costs nothing
  downstream. This is the recommended path.
- Removing it from the two files at `HEAD` stops it being trivially greppable going
  forward and is worth doing either way.

To stop recurrence, the useful control is at authoring time, not at review time: a
pre-commit or CI scan for IPv4 literals and `user:pass@host` URLs under `docs/`,
which is where operational notes accumulate.

## Acceptance

- The exposed endpoint is retired or the address no longer resolves to a home
  network.
- A new IPv4 literal or credential-shaped URL added under `docs/` fails a check
  before it is pushed.
