import {
  ChangeDetectionStrategy,
  Component,
  OnInit,
  computed,
  inject,
  signal,
} from '@angular/core';
import {ActivatedRoute, RouterLink} from '@angular/router';
import {DatePipe} from '@angular/common';
import {FormsModule} from '@angular/forms';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';

import {AppButtonComponent} from '../../ui/button';
import {AppIconComponent} from '../../ui/icon';
import {AppInputComponent} from '../../ui/input';
import {AppSelectComponent} from '../../ui/select';
import {AppSwitchComponent} from '../../ui/switch';
import {AppTextareaComponent} from '../../ui/textarea';
import {AppBadgeComponent} from '../../ui/badge';
import {AppSpinnerComponent} from '../../ui/spinner';
import {SidebarToggleComponent} from '../../shell/sidebar-toggle/sidebar-toggle.component';

interface SelectOption {
  value: string;
  label: string;
}

import {ApiService} from '../../core/services/api.service';
import {
  Automation,
  AutomationsService,
  CreateAutomationRequest,
} from '../../core/services/automations.service';
import {Expert} from '../../core/models/api.model';
import {CronPreviewComponent} from './cron-preview.component';
import {EscapeHatchPanelComponent} from './escape-hatch-panel.component';

type CronPreset =
  | 'every_day'
  | 'every_weekday'
  | 'every_monday'
  | 'every_month_first'
  | 'every_hour'
  | 'custom';

interface EditorState {
  mode: 'create' | 'edit';
  id: string | null;
  name: string;
  description: string;
  preset: CronPreset;
  time: string; // HH:MM (24h) for non-custom presets
  customCron: string;
  timezone: string;
  expert: string;
  prompt: string;
  enabled: boolean;
  autonomy: Automation['autonomy'];
  priority: number;
  maxFiresPerDay: number;
  catchupWindowHours: number;
  projectId: string | null;
  showAdvanced: boolean;
}

const SOFT_CAP = 20;

@Component({
  selector: 'app-automations-page',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    DatePipe,
    FormsModule,
    RouterLink,
    TranslocoPipe,
    AppButtonComponent,
    AppIconComponent,
    AppInputComponent,
    AppSelectComponent,
    AppSwitchComponent,
    AppTextareaComponent,
    AppBadgeComponent,
    AppSpinnerComponent,
    SidebarToggleComponent,
    CronPreviewComponent,
    EscapeHatchPanelComponent,
  ],
  templateUrl: './automations-page.component.html',
  styleUrl: './automations-page.component.scss',
})
export class AutomationsPageComponent implements OnInit {
  private readonly service = inject(AutomationsService);
  private readonly api = inject(ApiService);
  private readonly route = inject(ActivatedRoute);
  private readonly transloco = inject(TranslocoService);

  readonly automations = this.service.automations;
  readonly isLoading = this.service.isLoading;
  readonly experts = signal<Expert[]>([]);
  readonly errorMessage = signal<string | null>(null);
  readonly editor = signal<EditorState | null>(null);

  readonly expertOptions = computed<SelectOption[]>(() =>
    this.experts().map((e) => ({value: e.id, label: e.display_name})),
  );

  readonly presetOptions: SelectOption[] = [
    {value: 'every_day', label: 'every day'},
    {value: 'every_weekday', label: 'every weekday (Mon–Fri)'},
    {value: 'every_monday', label: 'every Monday'},
    {value: 'every_month_first', label: 'first day of the month'},
    {value: 'every_hour', label: 'every hour'},
    {value: 'custom', label: 'custom cron'},
  ];

  readonly autonomyOptions: SelectOption[] = [
    {value: 'full', label: 'full (never pause)'},
    {value: 'review', label: 'review after completion'},
    {value: 'partial', label: 'partial'},
    {value: 'guided', label: 'guided'},
    {value: 'dependent', label: 'dependent'},
  ];

  /** Pre-selected timezones. Falls back to the IANA browser default. */
  readonly timezoneOptions = computed<SelectOption[]>(() => {
    const browserTz = this._defaultTimezone();
    const common = [
      'UTC',
      'Europe/Berlin',
      'Europe/London',
      'America/New_York',
      'America/Los_Angeles',
      'Asia/Tokyo',
      'Australia/Sydney',
    ];
    const all = new Set<string>([browserTz, ...common]);
    return Array.from(all).map((tz) => ({value: tz, label: tz}));
  });

  readonly cronExpr = computed<string>(() => {
    const e = this.editor();
    if (!e) return '';
    return derivePresetCron(e.preset, e.time, e.customCron);
  });

  /** When at or above the soft cap we surface a warning above the list. */
  readonly softCapHit = computed(() => this.automations().length >= SOFT_CAP);

  ngOnInit(): void {
    this.service.loadMine();
    this.api.getExperts().subscribe({
      next: (list) => this.experts.set(list),
      error: () => this.experts.set([]),
    });

    // Project pre-fill: /automations?project=<uuid> opens the editor with
    // the project preselected — driven by the "Create automation" button
    // on a project's detail page.
    this.route.queryParamMap.subscribe((params) => {
      const projectId = params.get('project');
      if (projectId) {
        this.openEditor(null, projectId);
      }
    });
  }

  openEditor(automation: Automation | null, prefillProjectId: string | null = null): void {
    if (automation) {
      const {preset, time} = inferPresetFromCron(automation.cron_expr ?? '');
      this.editor.set({
        mode: 'edit',
        id: automation.id,
        name: automation.name,
        description: automation.description ?? '',
        preset,
        time,
        customCron: automation.cron_expr ?? '',
        timezone: automation.timezone,
        expert: automation.expert,
        prompt: automation.prompt,
        enabled: automation.enabled,
        autonomy: automation.autonomy,
        priority: automation.priority,
        maxFiresPerDay: automation.max_fires_per_day,
        catchupWindowHours: Math.round(automation.catchup_window_seconds / 3600),
        projectId: automation.project_id,
        showAdvanced: false,
      });
    } else {
      this.editor.set({
        mode: 'create',
        id: null,
        name: '',
        description: '',
        preset: 'every_day',
        time: '09:00',
        customCron: '',
        timezone: this._defaultTimezone(),
        expert: this.experts()[0]?.id ?? '',
        prompt: '',
        enabled: true,
        autonomy: 'review',
        priority: 5,
        maxFiresPerDay: 100,
        catchupWindowHours: 24,
        projectId: prefillProjectId,
        showAdvanced: false,
      });
    }
  }

  closeEditor(): void {
    this.editor.set(null);
    this.errorMessage.set(null);
  }

  updateEditor<K extends keyof EditorState>(field: K, value: EditorState[K]): void {
    const current = this.editor();
    if (!current) return;
    this.editor.set({...current, [field]: value});
  }

  saveEditor(): void {
    const e = this.editor();
    if (!e) return;
    const cron = this.cronExpr();
    if (!cron || !e.name.trim() || !e.expert || !e.prompt.trim()) {
      this.errorMessage.set(this.transloco.translate('automations.editor.requiredMissing'));
      return;
    }
    const body: CreateAutomationRequest = {
      name: e.name.trim(),
      description: e.description.trim() || null,
      cron_expr: cron,
      timezone: e.timezone,
      catchup_window_seconds: Math.max(0, e.catchupWindowHours * 3600),
      expert: e.expert,
      prompt: e.prompt.trim(),
      autonomy: e.autonomy,
      priority: e.priority,
      enabled: e.enabled,
      max_fires_per_day: e.maxFiresPerDay,
      project_id: e.projectId,
    };
    this.errorMessage.set(null);
    const op$ =
      e.mode === 'create'
        ? this.service.create(body)
        : this.service.update(e.id!, body);
    op$.subscribe({
      next: () => {
        this.closeEditor();
        this.service.loadMine();
      },
      error: (err) => {
        this.errorMessage.set(this._formatError(err));
      },
    });
  }

  toggleEnabled(row: Automation): void {
    const op$ = row.enabled ? this.service.pause(row.id) : this.service.resume(row.id);
    op$.subscribe({
      error: (err) => this.errorMessage.set(this._formatError(err)),
    });
  }

  runNow(row: Automation): void {
    this.service.runNow(row.id).subscribe({
      next: () => this.service.loadMine(),
      error: (err) => this.errorMessage.set(this._formatError(err)),
    });
  }

  remove(row: Automation): void {
    const confirmMessage = this.transloco.translate('automations.list.confirmDelete', {
      name: row.name,
    });
    if (!confirm(confirmMessage)) return;
    this.service.delete(row.id).subscribe({
      error: (err) => this.errorMessage.set(this._formatError(err)),
    });
  }

  private _defaultTimezone(): string {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
    } catch {
      return 'UTC';
    }
  }

  private _formatError(err: unknown): string {
    if (
      err &&
      typeof err === 'object' &&
      'error' in err &&
      err.error &&
      typeof (err as {error: {detail?: unknown}}).error === 'object'
    ) {
      const detail = (err as {error: {detail?: unknown}}).error.detail;
      if (typeof detail === 'string') return detail;
    }
    return this.transloco.translate('automations.errors.generic');
  }
}

/** Convert the preset picker state into a 5-field cron expression. */
export function derivePresetCron(
  preset: CronPreset,
  time: string,
  custom: string,
): string {
  if (preset === 'custom') return custom.trim();
  const [hh, mm] = time.split(':');
  const h = parseInt(hh ?? '0', 10);
  const m = parseInt(mm ?? '0', 10);
  if (Number.isNaN(h) || Number.isNaN(m)) return '';
  switch (preset) {
    case 'every_day':
      return `${m} ${h} * * *`;
    case 'every_weekday':
      return `${m} ${h} * * 1-5`;
    case 'every_monday':
      return `${m} ${h} * * 1`;
    case 'every_month_first':
      return `${m} ${h} 1 * *`;
    case 'every_hour':
      return `${m} * * * *`;
  }
}

/** Reverse-engineer the preset picker state from a stored cron expression.
 *  Falls back to 'custom' when the expression doesn't match a known
 *  preset shape — keeps round-trip editing predictable. */
export function inferPresetFromCron(expr: string): {preset: CronPreset; time: string} {
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return {preset: 'custom', time: '09:00'};
  const [m, h, dom, _mon, dow] = parts;
  const minute = m.padStart(2, '0');
  const hour = h.padStart(2, '0');
  const looksDaily = dom === '*' && dow === '*';
  if (m !== '*' && h === '*' && dom === '*' && dow === '*') {
    return {preset: 'every_hour', time: '00:00'};
  }
  if (looksDaily && /^\d+$/.test(m) && /^\d+$/.test(h)) {
    return {preset: 'every_day', time: `${hour}:${minute}`};
  }
  if (/^\d+$/.test(m) && /^\d+$/.test(h) && dom === '*' && dow === '1-5') {
    return {preset: 'every_weekday', time: `${hour}:${minute}`};
  }
  if (/^\d+$/.test(m) && /^\d+$/.test(h) && dom === '*' && dow === '1') {
    return {preset: 'every_monday', time: `${hour}:${minute}`};
  }
  if (/^\d+$/.test(m) && /^\d+$/.test(h) && dom === '1' && dow === '*') {
    return {preset: 'every_month_first', time: `${hour}:${minute}`};
  }
  return {preset: 'custom', time: `${hour}:${minute}`};
}
