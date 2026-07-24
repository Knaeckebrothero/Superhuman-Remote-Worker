---
guide_id: sessions.permissions-and-workspaces
content_type: how_to
capability_ids:
  - sessions.permission-mode
  - workspaces.select
journey_ids:
  - sessions.configure-permissions
  - sessions.choose-workspace
---

# Permissions, grants, workspaces, and unavailable features

“SRW supports this” and “this session can do it now” are different claims. A
feature can be:

1. implemented in the running product;
2. enabled and configured on this deployment;
3. allowed by the user's effective capability grants;
4. selected by the expert/session and compatible with its workspace;
5. connected to the required project or connector; and
6. ready and present in the agent's current tool list.

The static app guide can explain those layers, but it cannot inspect every
flag, grant, attachment, service, or loaded tool. For a current-state question,
trust an exact disabled-control reason, the session's visible tools and
settings, and resource readiness shown by Cockpit. If those do not identify the
layer, say that the cause is unknown and ask an administrator to check it; do
not turn “the tool is missing” into a made-up diagnosis.

## Permission mode is the approval policy

Persistent sessions currently have three modes:

- **Supervised** asks for approval before every tool call.
- **Auto-accept** runs non-shell tools without asking, but still asks for
  `run_command`, `shell_execute`, and `shell_read`.
- **Autonomous** runs tool calls without per-call approval.

New session experts default to Supervised unless an account, expert, or
per-session setting overrides it. Change the current session from its header
**Settings** panel; change the account fallback under
**Settings → Persistent Agent → Permission Mode**.

Permission mode does not add tools, attach connectors, make a service healthy,
or bypass authorization. Autonomous means “do not ask for each tool,” not
“ignore access controls.” Every real operation still enforces its own user,
project, connector, deployment, and safety checks. A user's
`permission_mode` grant is a ceiling; modes above it are unavailable even if
an expert requests them. The built-in non-admin ceiling allows Auto-accept;
Autonomous needs an explicit grant unless an administrator narrows or widens
the applicable policy.

## Capability grants are restrict-only ceilings

For non-admin users, effective grants combine global, project, and user scope.
More specific grants cannot widen a restriction imposed above them, and when
several attached projects contribute values the most restrictive result wins.
Admins bypass this grant policy, but not deployment kill switches, missing
services, ownership checks, or other operation-specific authorization.

The current grant catalog covers:

- `personal_default_experts` — set or fork personal default experts;
- `vm_workspace` — select or upgrade to a VM workspace;
- `shell_tools` — load shell tools;
- `delegation` — enable subagent delegation;
- `public_datasources` — publish a connector to all users;
- `email_autonomous_send` — allow fail-closed unattended email sending;
- `datasource_tools` — load connector-backed tools;
- `browser` — load the agent's direct browser tools;
- `model_selection` — restrict the selectable model set;
- `autonomy_ceiling` — cap worker-job autonomy; and
- `permission_mode` — cap persistent-session permission mode.

An unavailable choice may be greyed out with **Requires a capability grant**.
Administrators manage scoped values under **Admin → Grants**. Changing the UI
does not replace server enforcement: session creation, attachment,
provisioning, dispatch, and individual actions recheck the applicable policy.

## Choose the workspace for the work

The account default is under
**Settings → Persistent Agent → Default Workspace**. A new session can override
it under **Sessions → New Session → Agent Settings → Advanced → Workspace**.
The platform default is Virtual.

| Workspace | What it provides | Important limits |
|---|---|---|
| **Virtual** | Fast, durable cloud-backed file tools without starting a workspace container; file Canvas can work when the backing store is materializable by the orchestrator | No shell, direct browser, git, repository checkout, live application, or shared browser. A process-local development store cannot be presented on Canvas. |
| **Container** (`sandbox` internally) | Isolated files plus shell, git, repository checkout, and direct browser support; the current Protected Cloud session path also requires this tier | Slower cold start. Individual tools still depend on the expert, tool selection, grants, and service configuration. This is the proven live-app/shared-browser path. |
| **None** | Chat with no workspace | No workspace file, shell, direct browser, git, or repository tools. Web research, databases, and knowledge may still work when independently configured and attached. |
| **VM** | A full per-session virtual machine for work that needs VM isolation or sudo/root access | Per-session opt-in only; never a saved default. Requires the `vm_workspace` grant, operator enablement, and an available VM provisioner. Current file Canvas and shared-browser VM support are not promised. |

A running session's header **Settings** panel shows its current workspace and
only the supported upgrade buttons. Virtual can upgrade to Container; Virtual
or Container can offer VM when allowed. Workspace changes are upgrade-only:
to move down to Virtual or None, start a new session. A workspace upgrade does
not automatically enable a missing tool category.

## Tool selection and live changes

At session creation, **Agent Settings** shows the tool categories allowed by
the selected expert and workspace. Direct Browser, Shell, Git, connectors, and
other categories can still be removed by backend or grant checks.

For an already-running session, header **Settings** can change only four
closed tool groups live:

- **Canvas**;
- **Fleet Management**;
- **Experts & Skills**; and
- **Automations & Loops**.

Model, permission mode, narration, those groups, and supported connector
changes apply starting with the next response. Changing the model, tools, or
connectors can reset the conversation's prompt cache, making the next response
slower. A checked group is a request to load its known tools, not proof that
every underlying capability became available.

## Diagnose a missing capability

Use the narrowest observable check:

1. **Look for the actual tool or Cockpit action.** If the agent cannot see the
   required tool, it must not offer to perform that action.
2. **Read the control's reason.** A grant tooltip, workspace-required message,
   connector readiness state, or deployment-disabled message identifies a
   specific layer.
3. **Check the current session Settings.** Confirm the tool group and workspace;
   apply a supported upgrade if the work needs shell, git, repository checkout,
   direct browser, or shared browser.
4. **Check scope and attachment.** A connector existing in **Connectors** does
   not mean it is attached to this session or project, and “Testing” is not
   “Ready.”
5. **Escalate deployment state.** Hidden feature-off controls, missing
   transports, unavailable provisioners, and service failures need an
   administrator.

Do not recommend switching to Autonomous as a cure for a missing tool or grant.
It changes approval prompts only.
