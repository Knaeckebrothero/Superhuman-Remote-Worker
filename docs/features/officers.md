---
tags:
  - note
  - srw
  - SuperHumanRemoteWorker
  - feature
---
# Officers — consolidated reference

Consolidated on 28.07.2026 from all officer-related notes in the vault (sources at the bottom). Originals kept unchanged. This is the single reference to take to the SRW repository.

## Concept
- Officers are **async agents who manage worker agents**
- They run in parallel to the main loop to guide and control the team
- They periodically check the running agents and make sure they aren't stuck and stay on track

## Roles (legion structure)
- **Centurion** — guides the unit, defines the goal, manages the requirements
- **Tesserarius** — assigns and monitors the jobs
- **Optio** — deals with the failed or struggling agents
- **Legionaries** — the worker agents
- Broader theme mapping: worker agent = legionary; session/persistent agent = Centurion; a **Legat** runs in the background (that will be the one to talk to in the builder)
- Naming: the agent calls you legate and you call him pilus (short for pilus primus, the centurion of the first century)

## Staffing & scaling rules
- Every ~20 agents get officers
- Number of officers can be adjusted based on difficulty of the task
- Multiple officers of each type are possible per century

## Org structure: officers, centuries, projects
- Each officer gets his own century of agents
	- You give tasks to officers
	- They manage the tasks like projects, delegating the agents
- Every project gets officers

## Model tiering
- Every century is made of 9 to 17 small models with one centurion (top-tier model) and his 2 officers (medium models) who manage the legionaries (small models) that do the programming
- Century tiers in the coding legion: first-rank centuries are small models (8–20b), middle centuries are 70b models, the last centuries (the first three) are top-tier models — small agents code, middle agents take harder features, top-tier only jumps in when needed (e.g. big issues)
- Finetuning: start with one model finetuned for both, perhaps later use separate models for strategic/tactical, e.g. **Caesar and Centurion**

## Officer expert (default automation)
- Ship a default "fix stuck jobs" automation and give it an **"officer expert"** — a smarter model that can fix jobs when they are detected as stuck

## Open questions
- Maybe the critic who can already schedule different jobs is what we wanted officers to be?
	- (Current critic role: the critic checks when goals are reached, the dev implements stuff, the scholar comes up with new backlog ideas)
- OpenAI und GLM subscriptions could be the officers?
- Should projects be centuries?

## UI / theme context
- Legion overview like in the video game: see your agent fleet
	- Warlords Britannia — if you hit Tab you see the legion from above as round dots for every legionary

## Possibly the namesake inspiration
Es gibt 4 Typen von Offizieren (maps well onto assigning model tiers to roles):
1. Dumm und Faul → für Routine-Tätigkeiten geeignet
2. Dumm und Fleißig → sofort aussortieren, richten nur Schaden an
3. Klug und Fleißig → müssen in den Generalstab
4. Klug und Faul → müssen in die Oberste Heeresleitung

## Sources
- [[00_ToDo_Notes_Ideas_Combined]] — "Add Officers (async agents who manage worker agents)" (~line 712), "Add officers to the loop" (~446), "officer expert" (~468), "Each officer gets his own century" (~522), Coding Legion (~877), design decisions century composition (~1079), Caesar/Centurion finetune (~918), Warlords Britannia fleet view (~808)
- [[02_Ideas]] — "Es gibt 4 Typen von Offizieren" (~line 114)
- [[AI backlog! Kanban, Sprints, Projects!!!]] — critic/dev/scholar role split
