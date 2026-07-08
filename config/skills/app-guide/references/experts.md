# Experts — which agent for which task

An **expert** is a preconfigured agent role: a persona, a toolset, and working
instructions tuned for one kind of work. You pick an expert when you create a
job (and some experts power interactive sessions). If you're unsure, the
default worker config is a capable generalist, and the **Assistant** is the
default for sessions.

## The roster

| Expert | Use it for |
|---|---|
| **Assistant** | The default session agent — turn-by-turn collaboration on research, writing, analysis, planning, and light coding. |
| **Scholar** | Exploration and research — scans the web and codebases, runs experiments, produces a high volume of idea and finding artifacts. |
| **Critic** | Quality gatekeeping — reviews diffs and proposals, audits for problems, runs tests. Harsh and evidence-based by design. Also runs automatically as the built-in reviewer of other jobs. |
| **Developer** | Implementation — turns acceptance criteria into failing tests, then minimum code to pass, then refactors. Test-driven throughout. |
| **Curator** | Knowledge extraction — distills a job's findings into structured notes in the project knowledge base. Usually runs automatically alongside other jobs when enabled. |
| **Designer** | UI/UX design — self-contained HTML/CSS mockups and design specs for developers to implement. |
| **Design Studio** | The interactive version of Designer — iterate on mockups and specs with you in a session. |
| **Bug Hunter** | Adversarial QA — picks a surface and tries to break it. Every finding comes with a reproduction. Hunts bugs; doesn't fix them. |
| **Product QA** | Product-level audit — uses the app like a customer would and reports broken setup, missing pieces, and doc gaps as evidence-backed issues. |

Worker experts (Scholar, Critic, Developer, Curator, Designer, Bug Hunter,
Product QA) run autonomous jobs; Assistant and Design Studio are session
experts you chat with.

## Custom experts

Deployments can enable an expert editor in the cockpit where you create,
duplicate, edit, import, and export your own experts (persona, instructions,
and configuration). This is admin-enabled per deployment — if you don't see
an Experts editing area, your deployment ships the bundled roster only.

Projects can also carry their own experts, so a team shares tuned roles.
