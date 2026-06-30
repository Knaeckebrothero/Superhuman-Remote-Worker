import {
  Component,
  OnDestroy,
  OnInit,
  computed,
  inject,
  input,
  signal,
} from '@angular/core';
import {DatePipe} from '@angular/common';

import {ApiService} from '../../core/services/api.service';
import {ModelService} from '../../core/services/model.service';
import {
  Expert,
  Job,
  ProjectLoop,
  ProjectLoopStartRequest,
} from '../../core/models/api.model';
import {AppButtonComponent} from '../../ui/button';
import {AppInputComponent} from '../../ui/input';
import {AppSelectComponent} from '../../ui/select';
import {AppTextareaComponent} from '../../ui/textarea';
import {AppFormFieldComponent} from '../../ui/form-field';
import {AppSpinnerComponent} from '../../ui/spinner';

type LoopPreset = 'build' | 'write' | 'research';

/** Job statuses that won't change further. */
const TERMINAL_JOB_STATUSES = new Set(['completed', 'failed', 'cancelled']);

/** True while at least one job is still non-terminal (in flight). */
export function hasInFlightJob(jobs: Pick<Job, 'status'>[]): boolean {
  return jobs.some((j) => !TERMINAL_JOB_STATUSES.has(j.status));
}

/**
 * The loop has ended (stopped/completed/failed) but a job it spawned is still
 * finishing. Stopping is graceful — the in-flight job runs to completion and no
 * new jobs are queued — so the UI shows a wind-down notice during this window.
 */
export function isLoopWindingDown(
  loop: Pick<ProjectLoop, 'status'> | null,
  jobs: Pick<Job, 'status'>[],
): boolean {
  if (!loop) return false;
  const active = loop.status === 'running' || loop.status === 'paused';
  return !active && hasInFlightJob(jobs);
}

/**
 * Experts eligible to fill a loop rotation slot. Loops run worker jobs; bundled
 * experts carry no `expert_type`, and DB session experts are excluded.
 */
export function workerExpertsOnly<T extends {expert_type?: string}>(
  experts: T[],
): T[] {
  return experts.filter((e) => !e.expert_type || e.expert_type === 'worker');
}

/**
 * The effective rotation to send as `role_sequence`: the preset list, or the
 * custom slots with surrounding blanks trimmed (so an unfilled custom slot can
 * never ship and the API never sees an empty entry).
 */
export function buildRoleSequence(
  mode: 'preset' | 'custom',
  customSlots: string[],
  presetRoles: string[],
): string[] {
  const seq = mode === 'custom' ? customSlots : presetRoles;
  return seq.map((s) => s.trim()).filter(Boolean);
}

/**
 * Project Loop tab — start / monitor / control the project self-improvement
 * loop. Mounted by the project-detail page only when the Loop tab is open, so
 * its ngOnInit/ngOnDestroy bound the polling lifecycle to tab visibility.
 *
 * Experimental power-user feature; copy is intentionally plain English.
 * Design: docs/features/project_self_improvement_loop.md.
 */
@Component({
  selector: 'app-project-loop',
  standalone: true,
  imports: [
    DatePipe,
    AppButtonComponent,
    AppInputComponent,
    AppSelectComponent,
    AppTextareaComponent,
    AppFormFieldComponent,
    AppSpinnerComponent,
  ],
  template: `
    <div class="loop-tab">
      <div class="loop-intro">
        <h3>Self-improvement loop</h3>
        <p>
          Runs jobs continuously — the Scholar proposes approaches, the Critic
          selects, and the execution role builds — coordinating through the
          project knowledge base until the budget runs out. Experimental: best
          run on a project with a clear goal and (for coding) a repository data
          source attached.
        </p>
      </div>

      @if (loading()) {
        <div class="loop-loading"><app-spinner size="md" tone="accent" /></div>
      } @else {
        @if (loop(); as l) {
          @if (isActive()) {
            <!-- LIVE PANEL -->
            <div class="loop-card">
              <div class="loop-status-row">
                <span class="loop-badge" [attr.data-status]="l.status">{{ l.status }}</span>
                <span class="loop-step">
                  {{ currentRole() }} · job {{ l.total_jobs_run }}{{ l.max_iterations ? ' of ' + l.max_iterations : '' }}
                </span>
                @if (l.consecutive_failures > 0) {
                  <span class="loop-warn">{{ l.consecutive_failures }} consecutive failure(s)</span>
                }
              </div>
              <div class="loop-meta">
                <div><span class="k">Model</span><span class="v">{{ l.model || 'project default' }}</span></div>
                <div><span class="k">Workspace</span><span class="v">{{ l.workspace_backend === 'vm' ? 'VM (root)' : (l.workspace_backend || 'sandbox') }}</span></div>
                <div><span class="k">Sequence</span><span class="v">{{ l.role_sequence.join(' → ') }}</span></div>
                @if (l.remaining_iterations !== null) {
                  <div><span class="k">Iterations left</span><span class="v">{{ l.remaining_iterations }}</span></div>
                }
                @if (l.run_until) {
                  <div><span class="k">Runs until</span><span class="v">{{ l.run_until | date: 'short' }}</span></div>
                }
                @if (l.current_job_id) {
                  <div><span class="k">Current job</span><span class="v mono">{{ l.current_job_id.slice(0, 8) }}</span></div>
                }
              </div>
              <div class="loop-actions">
                @if (l.status === 'running') {
                  <app-button variant="secondary" size="sm" [disabled]="busy()" (clicked)="pause()">Pause</app-button>
                } @else {
                  <app-button variant="primary" size="sm" [disabled]="busy()" (clicked)="resume()">Resume</app-button>
                }
                <app-button variant="danger" size="sm" [disabled]="busy()" (clicked)="stop()">Stop</app-button>
              </div>
            </div>
          } @else if (windingDown()) {
            <!-- WINDING DOWN: loop stopped but the in-flight job is still finishing -->
            <div class="loop-outcome winding" [attr.data-status]="l.status">
              <strong>Loop {{ l.status }}.</strong>
              The job still running will finish on its own — no new jobs will be
              queued after it.
            </div>
          } @else {
            <!-- TERMINAL OUTCOME -->
            <div class="loop-outcome" [attr.data-status]="l.status">
              Last run: <strong>{{ l.status }}</strong>
              @if (l.stop_reason) { ({{ l.stop_reason }}) }
              — {{ l.total_jobs_run }} job(s) run.
              @if (l.last_error) { <span class="err">{{ l.last_error }}</span> }
            </div>
          }

          @if ((isActive() || windingDown()) && jobs().length) {
            <div class="loop-jobs">
              <div class="loop-jobs-title">Jobs this run ({{ jobs().length }})</div>
              @for (j of jobs(); track j.id) {
                <div class="loop-job">
                  <span class="role">{{ jobRole(j) }}</span>
                  <span class="loop-badge sm" [attr.data-status]="j.status">{{ j.status }}</span>
                  <span class="mono dim">{{ j.id.slice(0, 8) }}</span>
                </div>
              }
            </div>
          }
        }

        @if (showStartForm()) {
          <!-- START FORM (shown when no loop is active) -->
          <div class="loop-card">
            @if (modelOptions().length === 0) {
              <p class="loop-hint">No models are configured for this project — jobs will use the project default.</p>
            }
            <app-form-field
              label="Model"
              hint="Optional — overrides every role's model. Leave blank to use each expert's own model."
            >
              <app-select [value]="fModel()" (changed)="fModel.set($event ?? '')">
                <option value="">Don't overwrite expert defaults</option>
                @for (m of modelOptions(); track m) {
                  <option [value]="m">{{ m }}</option>
                }
              </app-select>
            </app-form-field>

            <app-form-field
              label="Workspace"
              hint="Where every role runs. Sandbox is a lightweight container; VM gives root + sudo (heavier, slower to boot)."
            >
              <app-select [value]="fBackend()" (changed)="fBackend.set($event ?? '')">
                <option value="">Sandbox container (default)</option>
                <option value="vm">Virtual machine — root + sudo</option>
              </app-select>
            </app-form-field>

            <app-form-field label="Cycle" hint="Which roles rotate, one job each, repeating.">
              <div class="loop-mode">
                <button
                  type="button"
                  class="loop-mode-btn"
                  [class.active]="fMode() === 'preset'"
                  (click)="setMode('preset')"
                >
                  Preset
                </button>
                <button
                  type="button"
                  class="loop-mode-btn"
                  [class.active]="fMode() === 'custom'"
                  (click)="setMode('custom')"
                >
                  Custom
                </button>
              </div>
            </app-form-field>

            @if (fMode() === 'preset') {
              <app-select [value]="fPreset()" (changed)="setPreset($event)">
                <option value="build">Build (scholar → critic → developer)</option>
                <option value="write">Write (scholar → critic → general)</option>
                <option value="research">Research (scholar → critic)</option>
              </app-select>
              <div class="loop-seq">
                @for (r of roleSequence(); track $index) {
                  <span class="loop-role-chip">{{ r }}</span>
                }
              </div>
            } @else {
              <p class="loop-hint">
                Pick an expert per step — they rotate one job each, repeating.
                Swap in a custom expert to change that step's model.
              </p>
              <div class="loop-slots">
                @for (slot of customSlots(); track $index) {
                  <div class="loop-slot">
                    <span class="loop-slot-n">{{ $index + 1 }}</span>
                    <app-select [value]="slot" (changed)="setSlot($index, $event)">
                      <option value="">— pick an expert —</option>
                      @for (e of workerExperts(); track e.id) {
                        <option [value]="slug(e)">{{ e.display_name }}</option>
                      }
                    </app-select>
                    <app-button
                      variant="ghost"
                      size="sm"
                      [disabled]="customSlots().length <= 1"
                      (clicked)="removeSlot($index)"
                    >
                      Remove
                    </app-button>
                  </div>
                }
              </div>
              <app-button variant="secondary" size="sm" (clicked)="addSlot()">
                Add step
              </app-button>
              @if (!workerExperts().length) {
                <p class="loop-hint">
                  No experts available — create a custom expert to pin a model.
                </p>
              }
            }

            <div class="loop-budget">
              <app-form-field label="Max iterations" hint="Total jobs to run (≈ this ÷ roles = cycles).">
                <app-input type="number" [value]="fMaxIterations()" (changed)="fMaxIterations.set($event)" />
              </app-form-field>
              <app-form-field label="Time limit (hours)" hint="Optional — also stop after this many hours.">
                <app-input type="number" [value]="fMaxHours()" (changed)="fMaxHours.set($event)" />
              </app-form-field>
            </div>

            <app-form-field label="Definition of done" hint="What 'finished' means — the Critic checks against this. Optional.">
              <app-textarea [rows]="2" [value]="fAcceptance()" (changed)="fAcceptance.set($event)" />
            </app-form-field>

            <app-form-field label="Extra steering" hint="Optional guidance folded into every job's brief.">
              <app-textarea [rows]="2" [value]="fUserPrompt()" (changed)="fUserPrompt.set($event)" />
            </app-form-field>

            @if (message()) { <p class="loop-msg">{{ message() }}</p> }

            <div class="loop-actions">
              <app-button variant="primary" [loading]="busy()" [disabled]="busy()" (clicked)="start()">
                Start loop
              </app-button>
            </div>
          </div>
        }
      }
    </div>
  `,
  styles: [
    `
      .loop-tab { display: flex; flex-direction: column; gap: 16px; max-width: 760px; }
      .loop-intro h3 { margin: 0 0 4px; font-size: 15px; color: var(--text-primary); }
      .loop-intro p { margin: 0; font-size: 13px; color: var(--text-secondary); line-height: 1.5; }
      .loop-loading { display: flex; justify-content: center; padding: 24px; }
      .loop-card {
        background: var(--panel-bg); border: 1px solid var(--border-color);
        border-radius: var(--radius-surface); padding: 16px;
        display: flex; flex-direction: column; gap: 14px;
      }
      .loop-status-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
      .loop-step { font-size: 13px; color: var(--text-primary); font-weight: 500; }
      .loop-warn { font-size: 12px; color: var(--warning); }
      .loop-badge {
        text-transform: capitalize; font-size: 11px; font-weight: 600;
        padding: 2px 8px; border-radius: var(--radius-tag);
        background: var(--surface-1); color: var(--text-secondary);
      }
      .loop-badge.sm { font-size: 10px; padding: 1px 6px; }
      .loop-badge[data-status='running'] { background: color-mix(in srgb, var(--info) 18%, transparent); color: var(--info); }
      .loop-badge[data-status='paused'] { background: color-mix(in srgb, var(--warning) 18%, transparent); color: var(--warning); }
      .loop-badge[data-status='completed'] { background: color-mix(in srgb, var(--success) 18%, transparent); color: var(--success); }
      .loop-badge[data-status='failed'] { background: color-mix(in srgb, var(--danger) 18%, transparent); color: var(--danger); }
      .loop-meta { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 8px 16px; }
      .loop-meta > div { display: flex; flex-direction: column; gap: 2px; }
      .loop-meta .k { font-size: 11px; color: var(--text-muted); }
      .loop-meta .v { font-size: 13px; color: var(--text-primary); }
      .mono { font-family: var(--font-mono, monospace); }
      .dim { color: var(--text-muted); }
      .loop-actions { display: flex; gap: 8px; }
      .loop-jobs {
        background: var(--panel-bg); border: 1px solid var(--border-color);
        border-radius: var(--radius-surface); padding: 12px 16px;
      }
      .loop-jobs-title { font-size: 12px; color: var(--text-muted); margin-bottom: 8px; }
      .loop-job { display: flex; align-items: center; gap: 10px; padding: 4px 0; font-size: 12px; }
      .loop-job .role { min-width: 90px; color: var(--text-primary); text-transform: capitalize; }
      .loop-outcome {
        font-size: 13px; color: var(--text-secondary);
        border: 1px solid var(--border-color); border-left: 3px solid var(--text-muted);
        border-radius: var(--radius-control); padding: 10px 12px;
      }
      .loop-outcome[data-status='completed'] { border-left-color: var(--success); }
      .loop-outcome[data-status='failed'] { border-left-color: var(--danger); }
      .loop-outcome.winding { border-left-color: var(--warning); }
      .loop-outcome .err { display: block; margin-top: 4px; color: var(--danger); font-size: 12px; }
      .loop-seq { display: flex; gap: 6px; flex-wrap: wrap; margin-top: -6px; }
      .loop-role-chip {
        font-size: 11px; padding: 2px 8px; border-radius: var(--radius-tag);
        background: var(--surface-1); color: var(--text-secondary); text-transform: capitalize;
      }
      .loop-mode { display: flex; gap: 6px; }
      .loop-mode-btn {
        font-size: 12px; padding: 4px 14px; border-radius: var(--radius-control);
        border: 1px solid var(--border-color); background: var(--surface-0);
        color: var(--text-secondary); cursor: pointer;
      }
      .loop-mode-btn.active {
        border-color: var(--accent-color); color: var(--text-primary);
        background: color-mix(in srgb, var(--accent-color) 15%, transparent);
      }
      .loop-slots { display: flex; flex-direction: column; gap: 8px; margin-bottom: 8px; }
      .loop-slot { display: flex; align-items: center; gap: 8px; }
      .loop-slot app-select { flex: 1; min-width: 0; }
      .loop-slot-n {
        flex: 0 0 auto; width: 20px; height: 20px; display: flex;
        align-items: center; justify-content: center; font-size: 11px;
        border-radius: 50%; background: var(--surface-1); color: var(--text-muted);
      }
      .loop-budget { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
      .loop-hint, .loop-msg { font-size: 12px; margin: 0; }
      .loop-hint { color: var(--text-muted); }
      .loop-msg { color: var(--warning); }
      @media (max-width: 768px) { .loop-budget { grid-template-columns: 1fr; } }
    `,
  ],
})
export class ProjectLoopComponent implements OnInit, OnDestroy {
  readonly projectId = input<string>('');

  private readonly api = inject(ApiService);
  private readonly modelService = inject(ModelService);

  readonly loop = signal<ProjectLoop | null>(null);
  readonly jobs = signal<Job[]>([]);
  readonly loading = signal(true);
  readonly busy = signal(false);
  readonly message = signal('');

  // Start-form state
  readonly fModel = signal('');
  // Per-loop workspace tier for every job: '' = default sandbox, 'vm' = root VM.
  readonly fBackend = signal('');
  readonly fPreset = signal<LoopPreset>('build');
  readonly fMaxIterations = signal('30');
  readonly fMaxHours = signal('');
  readonly fAcceptance = signal('');
  readonly fUserPrompt = signal('');
  // Cycle builder: 'preset' uses a ready-made rotation; 'custom' lets the user
  // pick an expert per step (each step's expert carries its own model).
  readonly fMode = signal<'preset' | 'custom'>('preset');
  readonly customSlots = signal<string[]>([]);
  // Worker experts available to fill custom slots (bundled + DB worker experts).
  readonly workerExperts = signal<Expert[]>([]);

  private pollHandle: ReturnType<typeof setInterval> | null = null;

  private readonly presets: Record<LoopPreset, string[]> = {
    build: ['scholar', 'critic', 'developer'],
    write: ['scholar', 'critic', 'default'],
    research: ['scholar', 'critic'],
  };

  readonly modelOptions = computed(() =>
    this.modelService.models().flatMap((g) => g.models),
  );
  // Effective rotation sent to the API: the preset list, or the custom slots
  // with blanks trimmed (so an unfilled custom slot can never ship).
  readonly roleSequence = computed(() =>
    buildRoleSequence(
      this.fMode(),
      this.customSlots(),
      this.presets[this.fPreset()],
    ),
  );
  readonly isActive = computed(() => {
    const l = this.loop();
    return !!l && (l.status === 'running' || l.status === 'paused');
  });
  readonly currentRole = computed(() => {
    const l = this.loop();
    if (!l || !l.role_sequence.length) return '';
    return l.role_sequence[l.seq_index % l.role_sequence.length];
  });

  /**
   * Loop is over (stopped/completed/failed) but its last job is still
   * finishing — stopping is graceful, so the in-flight job runs to completion
   * and no new jobs are queued. Drives the wind-down disclaimer.
   */
  readonly windingDown = computed(() =>
    isLoopWindingDown(this.loop(), this.jobs()),
  );
  /** Offer the start form only when nothing is active or winding down. */
  readonly showStartForm = computed(
    () => !this.isActive() && !this.windingDown(),
  );

  ngOnInit(): void {
    const pid = this.projectId();
    if (pid) this.modelService.load(pid);
    // Worker experts for the custom cycle builder. Loops run worker jobs;
    // bundled experts carry no type, DB session experts are filtered out.
    this.api.getExperts().subscribe((experts) => {
      this.workerExperts.set(workerExpertsOnly(experts));
    });
    this.refresh();
    // Live poll while the tab is open; cleared on destroy (tab close / nav away).
    this.pollHandle = setInterval(() => this.refresh(true), 10000);
  }

  ngOnDestroy(): void {
    if (this.pollHandle) clearInterval(this.pollHandle);
  }

  refresh(silent = false): void {
    const pid = this.projectId();
    if (!pid) {
      this.loading.set(false);
      return;
    }
    if (!silent) this.loading.set(true);
    this.api.getProjectLoop(pid).subscribe((l) => {
      this.loop.set(l);
      this.loading.set(false);
      // Fetch jobs while active — and while stopped, since a graceful stop
      // leaves the in-flight job finishing (needed for the wind-down notice).
      if (
        l &&
        (l.status === 'running' ||
          l.status === 'paused' ||
          l.status === 'stopped')
      ) {
        this.api.listProjectLoopJobs(pid).subscribe((j) => this.jobs.set(j));
      } else {
        this.jobs.set([]);
      }
    });
  }

  setPreset(value: string | null): void {
    if (value === 'build' || value === 'write' || value === 'research') {
      this.fPreset.set(value);
    }
  }

  setMode(mode: 'preset' | 'custom'): void {
    // Seed the custom slots from the current preset the first time in, so the
    // user starts from a sensible rotation instead of a blank list.
    if (mode === 'custom' && !this.customSlots().length) {
      this.customSlots.set([...this.presets[this.fPreset()]]);
    }
    this.fMode.set(mode);
  }

  /** Slug to reference an expert by name in role_sequence (DB: name; bundled: id). */
  slug(e: Expert): string {
    return e.name ?? e.id;
  }

  setSlot(i: number, value: string | null): void {
    const next = [...this.customSlots()];
    next[i] = value ?? '';
    this.customSlots.set(next);
  }

  addSlot(): void {
    const first = this.workerExperts()[0];
    this.customSlots.set([...this.customSlots(), first ? this.slug(first) : '']);
  }

  removeSlot(i: number): void {
    this.customSlots.set(this.customSlots().filter((_, idx) => idx !== i));
  }

  jobRole(j: Job): string {
    return j.config_name || 'job';
  }

  start(): void {
    const pid = this.projectId();
    if (!pid) return;
    this.message.set('');

    if (!this.roleSequence().length) {
      this.message.set('Add at least one expert to the cycle.');
      return;
    }

    const body: ProjectLoopStartRequest = {
      model: this.fModel() || null,
      workspace_backend: this.fBackend() || null,
      role_sequence: this.roleSequence(),
      acceptance_criteria: this.fAcceptance().trim() || null,
      user_prompt: this.fUserPrompt().trim() || null,
      max_consecutive_failures: 3,
    };
    const maxIter = parseInt(this.fMaxIterations(), 10);
    if (Number.isFinite(maxIter) && maxIter > 0) body.max_iterations = maxIter;
    const hours = parseFloat(this.fMaxHours());
    if (Number.isFinite(hours) && hours > 0) {
      body.run_until = new Date(Date.now() + hours * 3_600_000).toISOString();
    }
    if (body.max_iterations == null && body.run_until == null) {
      this.message.set('Set a max iterations and/or a time limit.');
      return;
    }

    this.busy.set(true);
    this.api.startProjectLoop(pid, body).subscribe((l) => {
      this.busy.set(false);
      if (l) {
        this.loop.set(l);
        this.message.set('');
        this.refresh(true);
      } else {
        this.message.set(
          'Could not start the loop — check that no loop is already running and the budget is valid.',
        );
      }
    });
  }

  pause(): void {
    this.act(this.api.pauseProjectLoop(this.projectId()));
  }

  resume(): void {
    this.act(this.api.resumeProjectLoop(this.projectId()));
  }

  stop(): void {
    this.act(this.api.stopProjectLoop(this.projectId()));
  }

  private act(obs: ReturnType<ApiService['pauseProjectLoop']>): void {
    this.busy.set(true);
    obs.subscribe((l) => {
      this.busy.set(false);
      if (l) this.loop.set(l);
      this.refresh(true);
    });
  }
}
