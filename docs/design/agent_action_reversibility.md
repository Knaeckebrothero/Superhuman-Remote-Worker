---
tags:
  - agent-architecture
  - security
  - design-principle
  - cloud
related:
  - "[[sudo_permissions]]"
  - "[[sudo_approval_gate]]"
  - "[[guardrails]]"
  - "[[openclaw_research]]"
  - "[[multi_datasource_support]]"
  - "[[cloud_workspace]]"
  - "[[nats_subject_acl_hardening]]"
aliases:
  - ["Action Reversibility", "Undo over Confirmation", "Reversible Agent Actions"]
---

# Agent Action Reversibility — design doc

**Status:** draft, 2026-05-25
**Scope:** the authorization *philosophy* for agent actions on user data (cloud files, databases, outbound channels). Not a feature — a principle the existing mechanisms ([[sudo_approval_gate]], [[guardrails]]) and future cloud-autonomy work should be measured against.

---

## TL;DR

Don't gate actions by prompting the user; **allow the action and make it reversible**. The standard HCI rule (prefer undo over confirmation dialogs) is right, but it has a precondition the desktop era hid: *undo only works in a reversible world*. An agent reaching into cloud storage, databases, and outbound channels breaks that closed-world assumption.

So the design rule is:

1. The safety question is **not** "how often is the agent right" (99% is the wrong metric — the worst 1% is where the damage lives). It is **"is the worst action it can take reversible?"**
2. Organize the agent's action surface by **reversible vs. irreversible**, *not* by files vs. databases.
3. **Convert as much as possible from irreversible into reversible** (snapshot-before-write, transactions, soft-delete, staging). Widen the "let it run + undo" surface until only a genuine residue remains.
4. Reserve the **deliberate gate** ([[sudo_approval_gate]]-style escalation) for that residue — the irreversible-and-escaping actions (disclosure, external side effects) — not for routine edits.

This is the unifying frame above our point mechanisms: the sudo gate, loop detection, and outbound restrictions are all answers to "when do we gate?" — this doc says *gate the irreversible residue, make everything else reversible instead.*

---

## 1. Motivation — the gap this builds in

Why is autonomous action on a user's own data a place a self-hosted project can win, when OpenAI/Google/Apple have orders of magnitude more resources? Because there are two different gaps, and they behave oppositely under spending:

- **Competence gap** ("not good enough yet"). Money closes this. More iteration, more eval, more polish. We do not win here and don't need to — we rent the frontier models.
- **Incentive gap** ("won't do the thing users want"). Money *widens* this. A $3T company spends its resources on not-getting-sued, not-leaking-data, and protecting the core business. The behaviors that make an assistant useful on your data — reaching into it aggressively and acting autonomously — are exactly what their legal/PR/privacy apparatus exists to prevent.

**Integration is theater.** Every assistant ships the Google Drive / cloud *connector* (OAuth, green checkmark) and then the only thing it can actually do is let you *manually attach one file*. That is not a half-finished feature; it is a **liability firewall**. By making the user pick the file, they made the user the decision-maker. Indexing a user's whole namespace and letting an autonomous agent act on it has an unbounded blast radius — at a billion users that's a certainty and a lawsuit, so they amputate the autonomy on the dangerous end.

**Self-hosting relocates the risk-acceptance authority** — and that, not privacy, is the unlock. (The privacy-paradox is real; people give away their data regardless, so "we respect your privacy" sells nothing.) A hyperscaler cannot let an individual user click "yes, I accept that my agent might do something dumb with all my files," because legally *they* hold the bag for everyone. On the user's own hardware, *the user* holds the bag and can authorize the exact capability the incumbents are structurally forbidden to offer.

But accepting the risk is not the same as engineering it away. The blast radius is just as real for us — we simply get to *accept* it rather than be protected from it. The work this doc describes is what makes accepting it sane: **reversibility is the mechanism that turns "the user accepts the risk" from reckless into reasonable.**

> Positioning note: deeper competitive context lives in [[openclaw_research]] (architecture, security record) and `docs/coding_agent_competitive_analysis.md`. This section is only the *why* behind the reversibility model below.

## 2. Principle: undo over confirmation

The HCI heuristic (Nielsen's "user control and freedom"; Raskin's "never use a warning when you mean undo") says: don't block the user with confirmation prompts — that's the same friction as the file picker. Allow any action, and provide a way back.

The catch: that principle was forged in **closed, reversible systems** — text editors, Photoshop, the file manager — where every action lives inside the app and the worst case is bounded. An agent with cloud reach, a database connection, and an outbound channel is not a closed system. Some of its actions leave our trust boundary and cannot be taken back.

Therefore:

- **Design for the tail, not the average.** A self-driving car that is right 99% of the time is a coffin. "The agent does the right thing 99% of the time" is not a safety argument; it says nothing about whether the 1% is recoverable.
- **The precondition for "allow + undo" is reversibility.** Where an action is reversible, allow it freely. Where it isn't, either make it reversible (§4) or gate it (§5) — never wave it through on the strength of the average.

## 3. The action taxonomy: reversible vs. irreversible

The useful axis is **not** files-vs-databases (our first instinct). A database write wrapped in a transaction is more reversible than an unversioned file overwrite. The axis is reversibility, and it has one engineered middle class:

| Class | Examples | Why | Default policy |
|---|---|---|---|
| **Reversible & contained** | edit / create / move / delete a file in a versioned store; rewrite a doc; reorganize a folder; workspace scratch work | State change stays inside a store we control and can roll back | **Allow, no prompt.** Versioned undo. |
| **Made reversible** (the goal) | DB write wrapped in a snapshot/transaction; soft-delete; staged change awaiting commit | Irreversible *by nature*, but we wrapped it so it can be undone | **Allow, no prompt.** Undo via snapshot/rollback. |
| Irreversible — **destructive** | hard `DROP`/`TRUNCATE`; overwrite with no version history; delete past retention window | Bytes are gone; no restore point exists | Convert to *Made reversible* first; else **deliberate escalation**. |
| Irreversible — **escaping** | send email / chat message; external API call; payment; public post; outbound webhook | Effect left our trust boundary; can't be recalled | **Deliberate escalation.** Dry-run preview; rate-limit. |
| Irreversible — **disclosure** | confidential data dropped into a log, a third-party tool, or the model-provider's context | Information disclosure can't be undone even by deleting our copy | Hardest case. Minimize/redact at the boundary; **not solved by undo**. |

The quiet killer is the last row. A git-backed undo log is a brilliant default for the first two classes and a **fig leaf over disclosure**: you cannot `git revert` an email that's been sent, an API call that moved money, or a secret the agent read and copied into a log line that shipped to the model provider. Reversibility solves *destruction*. It does nothing for *exfiltration*. Any design that conflates "we have undo" with "we are safe" has this hole.

## 4. Engineering goal: convert irreversible → reversible

Most of the work is here. Rather than asking "should we block this?", ask **"can we make this undoable?"** Every conversion moves an action up the table into the no-prompt zone:

- **Databases:** wrap destructive writes in a transaction with explicit commit; snapshot-before-write for schema/bulk changes; default the agent's credentials to **read-only**, with write access as a scoped, time-boxed escalation (see ZSP / JIT model in [[sudo_permissions]]).
- **Files:** soft-delete and versioning instead of hard delete/overwrite (most stores give this for free — see §6).
- **Batch / multi-step:** stage changes and commit atomically, so a regret rolls back the whole set instead of leaving half-applied state.

**The excavator principle.** The objection "if the agent deletes your DB, you were dumb to give it write access" is a trap: that *is* prevention, just moved to setup time, and it contradicts "never block the user." A real excavator doesn't run on "the operator is an expert, so it's fine" — it's covered in interlocks (load limits, hydraulic lockouts, two-lever arming, exclusion zones) precisely so even a pro can't one-shot a catastrophe. The analogy argues *for building the lockout* — read-only-by-default creds, dry-run, transaction-wrap, snapshot-before-destructive — not for handing the user all the responsibility.

## 5. The irreversible residue: deliberate escalation

After §4, what's left is the genuinely irreversible: **escaping** and **disclosure** actions. These get the heavier treatment — and only these. Routine file edits must never hit it, or we recreate approval fatigue (the exact friction [[sudo_permissions]] is fighting).

The escalation primitive already exists: the [[sudo_approval_gate]] intercepts privileged actions and either auto-approves by rule or holds for a human decision, and [[sudo_permissions]] is extending it toward Zero Standing Privileges, JIT grants, and command categories. **Reuse it for the irreversible residue**, governed by the same least-privilege / non-human-identity principles:

- Outbound send (email, chat, public post) → gated, with a dry-run/preview of exactly what will be sent.
- External API with real-world effect (payment, booking, webhook) → gated, rate-limited.
- Disclosure → the residual hard problem; see §6.

The orchestrator already exposes the human side of this loop (`approve_sudo_request` / `deny_sudo_request` / `list_sudo_requests`). The design task is to route the *escaping/disclosure* classes through it while keeping the reversible classes prompt-free.

## 6. Mechanism notes by resource

**Agent's own workspace.** Git is genuinely perfect here — it already tracks the agent's working copy. Keep using it for workspace-local undo (see [[git]]). This is the cleanest reversible surface we have.

**User's cloud namespace (Drive / OpenCloud).** Do **not** build a git mirror of the user's whole cloud namespace to get undo — that reinvents versioning the store already ships, and git handles large/binary/many-file namespaces poorly. Instead:
- Lean on the store's **native versions + trash**. Google Drive keeps version history and a trash; OpenCloud / oCIS ships file versions and a trashbin (confirm it's exposed in our deployment config).
- Use our **audit trail** to map *regret → restore*: "agent did X to file Y at version Z" → restore the prior version. We already capture rich audit data; the missing piece is the index from an audited action to the store-native restore point, plus a UX to trigger it.

**Databases.** Covered in §4 — transaction-wrap, snapshot-before-destructive, read-only default, write-as-escalation. Whole-DB backup is *not* the reversibility mechanism (too coarse, too slow); per-operation snapshots/transactions are.

**Outbound / disclosure.** The residual hard problem, and reversibility does not reach it. Borrow the defenses catalogued in [[openclaw_research]] §8: treat fetched/external content as hostile, redact credential-shaped strings before any outbound action, wrap untrusted content in explicit boundary markers, and remember OpenClaw's own conclusion — *"prompt injection is not solved."* Transport-level scoping of who-can-send-what is tracked separately in [[nats_subject_acl_hardening]].

## 7. Relationship to existing mechanisms

| Mechanism | Relationship to this principle |
|---|---|
| [[sudo_approval_gate]] / [[sudo_permissions]] | The **escalation primitive** for the irreversible residue (§5). A consumer of this principle, not the principle itself. |
| [[guardrails]] / [[guardrails_roadmap]] | **Orthogonal.** Those keep the agent *on-task* during long jobs (focus/drift). This doc is about *action safety on external state*. Don't conflate them despite the shared word. |
| [[openclaw_research]] §6/§8/§10 | OpenClaw's exec-approval modes (`deny`/`ask`/`full`), Docker sandbox, and outbound-send gates are **point solutions**. This doc is the unifying frame they're instances of. |
| Loop detection / hard caps | Bound runaway *cost/volume*; complementary to bounding *irreversibility*. Both are tail-risk controls. |

## 8. Open questions

1. **Where exactly is the "disclosure" trust boundary?** Logs, the model provider's context, third-party tool calls, and downstream MCP servers are all exits. Which are in scope for redaction/minimization, and which are accepted risk?
2. **Per-datasource reversibility registry.** Should each attached datasource ([[multi_datasource_support]]) declare its reversibility capability (versioned? transactional? append-only? write-once?) so the agent can pick the right default policy automatically?
3. **Regret → restore UX.** How do we surface "here is what the agent did, and here is the one-click undo" from the audit trail + native versions? This is the feature that makes the whole model trustworthy to a user.
4. **Where does snapshot-before-write live** — in the tool wrapper, the datasource adapter, or the workspace backend? Wherever it goes, it must be impossible for a tool to perform a destructive write that bypasses it.
5. **Default credential posture.** Confirm read-only-by-default is enforced at credential-issuance, not merely requested politely of the agent.

## 9. References

Internal:
- [[sudo_permissions]], [[sudo_approval_gate]], [[sudo_approval_plugin]] — the escalation gate this principle reuses.
- [[openclaw_research]] §6 (loop/exec), §8 (security model, "prompt injection is not solved"), §10 (outbound send gates).
- [[guardrails]], [[guardrails_roadmap]] — orthogonal focus-guardrails (named similarly, different concern).
- [[multi_datasource_support]], [[cloud_workspace]], [[git]], [[nats_subject_acl_hardening]].

External anchors:
- HCI: Nielsen, "User control and freedom" (undo/redo over confirmation); Raskin, *The Humane Interface* ("never use a warning when you mean undo").
- The incentive-gap framing was prompted by the OpenClaw cost story (~$1.3M/mo, OpenAI-funded research run) and the Antigravity 2.0 I/O demo — both illustrate that spend buys capability, not the thing users want.
