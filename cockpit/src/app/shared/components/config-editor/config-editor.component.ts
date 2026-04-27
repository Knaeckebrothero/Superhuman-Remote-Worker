import { Component, effect, inject, input, output, signal, OnInit } from '@angular/core';
import { NgTemplateOutlet } from '@angular/common';
import { HttpClient } from '@angular/common/http';
import { TranslocoPipe, TranslocoService } from '@jsverse/transloco';
import { AppIconButtonComponent } from '../../../ui/icon-button';
import { AppBadgeComponent } from '../../../ui/badge';
import { AppChipComponent } from '../../../ui/chip';
import { AppInputComponent } from '../../../ui/input';
import { AppSelectComponent } from '../../../ui/select';
import { AppTextareaComponent } from '../../../ui/textarea';
import { AppCheckboxComponent } from '../../../ui/checkbox';

// =============================================================================
// Schema Node Model
// =============================================================================

interface SchemaNode {
  path: string;
  key: string;
  type: 'string' | 'number' | 'integer' | 'boolean' | 'object' | 'array';
  nullable: boolean;
  title: string;
  description: string;
  defaultValue: unknown;
  enumValues?: string[];
  minimum?: number;
  maximum?: number;
  children?: SchemaNode[];
  isSection: boolean;
}

// =============================================================================
// Helpers
// =============================================================================

const SKIP_KEYS = new Set([
  '$schema', 'agent_id', 'display_name', 'description', 'icon', 'color', 'tags',
  'tools', 'instruction_files', 'connections',
]);

function humanize(key: string): string {
  return key
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function resolveType(rawType: unknown): { type: string; nullable: boolean } {
  if (Array.isArray(rawType)) {
    const nullable = rawType.includes('null');
    const primary = rawType.find((t: string) => t !== 'null') || 'string';
    return { type: primary, nullable };
  }
  return { type: (rawType as string) || 'string', nullable: false };
}

function parseSchema(
  schema: any,
  defs: Record<string, any>,
  parentPath: string,
  skipKeys: Set<string>,
): SchemaNode[] {
  if (!schema?.properties) return [];
  const nodes: SchemaNode[] = [];

  for (const [key, rawProp] of Object.entries<any>(schema.properties)) {
    if (skipKeys.has(key)) continue;
    const fullPath = parentPath ? `${parentPath}.${key}` : key;

    // Resolve $ref
    let prop = rawProp;
    if (rawProp.$ref) {
      const refName = rawProp.$ref.replace('#/$defs/', '');
      const resolved = defs[refName];
      if (resolved) {
        // Merge: rawProp fields (like description) override the ref
        prop = { ...resolved, ...rawProp, $ref: undefined };
        // But use resolved properties if rawProp doesn't have them
        if (!prop.properties && resolved.properties) {
          prop.properties = resolved.properties;
        }
        if (!prop.type && resolved.type) {
          prop.type = resolved.type;
        }
      }
    }

    const { type, nullable } = resolveType(prop.type);

    const node: SchemaNode = {
      path: fullPath,
      key,
      type: type as SchemaNode['type'],
      nullable,
      title: humanize(key),
      description: prop.description || '',
      defaultValue: prop.default,
      enumValues: prop.enum?.filter((v: unknown) => v !== null),
      minimum: prop.minimum,
      maximum: prop.maximum,
      isSection: type === 'object' && !!prop.properties,
    };

    if (type === 'object' && prop.properties) {
      node.children = parseSchema(prop, defs, fullPath, new Set());
    }

    nodes.push(node);
  }

  return nodes;
}

/** Flatten nested object to flat path→value map. */
function flattenObject(obj: Record<string, unknown>, prefix = ''): Map<string, unknown> {
  const result = new Map<string, unknown>();
  for (const [key, value] of Object.entries(obj)) {
    const path = prefix ? `${prefix}.${key}` : key;
    if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
      const nested = flattenObject(value as Record<string, unknown>, path);
      nested.forEach((v, k) => result.set(k, v));
    } else {
      result.set(path, value);
    }
  }
  return result;
}

/** Build nested object from flat path→value map. */
function buildNestedObject(flatMap: Map<string, unknown>): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const [path, value] of flatMap) {
    const parts = path.split('.');
    let current: any = result;
    for (let i = 0; i < parts.length - 1; i++) {
      if (!(parts[i] in current) || typeof current[parts[i]] !== 'object' || Array.isArray(current[parts[i]])) {
        current[parts[i]] = {};
      }
      current = current[parts[i]];
    }
    current[parts[parts.length - 1]] = value;
  }
  return result;
}

/** Get a nested value from an object by dot-path. */
function getNestedValue(obj: unknown, path: string): unknown {
  if (!obj || typeof obj !== 'object') return undefined;
  return path.split('.').reduce((cur: any, key) => cur?.[key], obj);
}

// =============================================================================
// Component
// =============================================================================

@Component({
  selector: 'app-config-editor',
  standalone: true,
  imports: [
    NgTemplateOutlet,
    TranslocoPipe,
    AppIconButtonComponent,
    AppBadgeComponent,
    AppChipComponent,
    AppInputComponent,
    AppSelectComponent,
    AppTextareaComponent,
    AppCheckboxComponent,
  ],
  template: `
    @if (!schemaLoaded()) {
      <div class="loading">{{ 'configEditor.loading' | transloco }}</div>
    } @else {
      <!-- Mode toggle -->
      <div class="mode-toggle">
        <app-chip
          size="sm"
          [selected]="mode() === 'visual'"
          (clicked)="switchMode('visual')"
        >
          {{ 'configEditor.modeVisual' | transloco }}
        </app-chip>
        <app-chip
          size="sm"
          [selected]="mode() === 'json'"
          (clicked)="switchMode('json')"
        >
          {{ 'configEditor.modeJson' | transloco }}
        </app-chip>
        <span class="override-count" [class.has-overrides]="overrideCount() > 0">
          {{ (overrideCount() === 1 ? 'configEditor.overrideSingle' : 'configEditor.overridePlural') | transloco: {n: overrideCount()} }}
        </span>
      </div>

      @if (mode() === 'visual') {
        <div class="visual-editor">
          @for (node of schemaNodes(); track node.path) {
            @if (node.isSection) {
              <div class="config-section">
                <button type="button" class="section-header" (click)="toggleSection(node.path)">
                  <span class="section-title">{{ node.title }}</span>
                  @if (sectionOverrideCount(node.path) > 0) {
                    <app-badge tone="accent" appearance="solid" size="xs" shape="pill">
                      {{ sectionOverrideCount(node.path) }}
                    </app-badge>
                  }
                  <span class="material-symbols-outlined toggle-icon">
                    {{ isSectionExpanded(node.path) ? 'expand_less' : 'expand_more' }}
                  </span>
                </button>
                @if (node.description && !isSectionExpanded(node.path)) {
                  <div class="section-desc">{{ node.description }}</div>
                }
                @if (isSectionExpanded(node.path) && node.children) {
                  <div class="section-body">
                    @for (child of node.children; track child.path) {
                      @if (child.isSection && child.children) {
                        <!-- Nested subsection (e.g., response_validation, proxy) -->
                        <div class="config-subsection">
                          <button type="button" class="subsection-header" (click)="toggleSection(child.path)">
                            <span>{{ child.title }}</span>
                            <span class="material-symbols-outlined toggle-icon sub">
                              {{ isSectionExpanded(child.path) ? 'expand_less' : 'expand_more' }}
                            </span>
                          </button>
                          @if (isSectionExpanded(child.path) && child.children) {
                            <div class="subsection-body">
                              @for (leaf of child.children; track leaf.path) {
                                <ng-container
                                  [ngTemplateOutlet]="fieldTpl"
                                  [ngTemplateOutletContext]="{ $implicit: leaf }"
                                />
                              }
                            </div>
                          }
                        </div>
                      } @else {
                        <ng-container
                          [ngTemplateOutlet]="fieldTpl"
                          [ngTemplateOutletContext]="{ $implicit: child }"
                        />
                      }
                    }
                  </div>
                }
              </div>
            } @else {
              <!-- Top-level non-object (e.g., autonomy) -->
              <ng-container
                [ngTemplateOutlet]="fieldTpl"
                [ngTemplateOutletContext]="{ $implicit: node }"
              />
            }
          }
        </div>
      } @else {
        <!-- JSON mode -->
        <div class="json-mode">
          <app-textarea
            class="json-editor"
            [value]="jsonText()"
            (valueChange)="onJsonEdit($event)"
            [rows]="20"
          />
          @if (jsonError()) {
            <div class="json-error">{{ jsonError() }}</div>
          }
          <span class="field-hint">
            {{ 'configEditor.jsonHint' | transloco }}
          </span>
        </div>
      }

      <!-- Reusable field template -->
      <ng-template #fieldTpl let-node>
        <div class="config-field" [class.modified]="isModified(node.path)">
          <div class="field-label">
            @if (isModified(node.path)) {
              <span class="modified-dot"></span>
            }
            <span>{{ node.title }}</span>
            @if (node.nullable) {
              <app-badge tone="neutral" size="xs">{{ 'configEditor.nullable' | transloco }}</app-badge>
            }
          </div>

          <!-- String with enum -->
          @if (node.type === 'string' && node.enumValues?.length) {
            <app-select
              [value]="$any(getDisplayValue(node.path, node.defaultValue)) ?? '__null__'"
              (changed)="setSmartOverride(node.path, $event === '__null__' ? null : $event, node.defaultValue)"
            >
              @if (node.nullable || node.defaultValue === undefined) {
                <option value="__null__">{{ (node.nullable ? 'configEditor.defaultAutoDetect' : 'configEditor.defaultOption') | transloco }}</option>
              }
              @for (opt of node.enumValues; track opt) {
                <option [value]="opt">{{ opt }}</option>
              }
            </app-select>
          }

          <!-- String (plain) -->
          @else if (node.type === 'string' && !node.enumValues?.length) {
            <app-input
              type="text"
              [value]="$any(getDisplayValue(node.path, node.defaultValue) ?? '')"
              (valueChange)="setSmartOverride(node.path, $event || null, node.defaultValue)"
            />
          }

          <!-- Number/integer with slider (min+max defined, range ≤ 10, NOT nullable, has default) -->
          @else if ((node.type === 'number' || node.type === 'integer') && node.minimum != null && node.maximum != null && (node.maximum - node.minimum) <= 10 && !node.nullable && node.defaultValue !== undefined) {
            <div class="slider-row">
              <input
                type="range"
                class="form-range"
                [min]="node.minimum"
                [max]="node.maximum"
                [step]="node.type === 'integer' ? 1 : 0.05"
                [value]="$any(getDisplayValue(node.path, node.defaultValue) ?? node.minimum)"
                (input)="setSmartOverride(node.path, +asInputValue($event), node.defaultValue)"
              />
              <span class="slider-value">{{ getDisplayValue(node.path, node.defaultValue) ?? '-' }}</span>
              @if (isModified(node.path)) {
                <app-icon-button
                  variant="danger"
                  size="sm"
                  type="button"
                  [ariaLabel]="'configEditor.resetToDefault' | transloco"
                  [tooltip]="'configEditor.resetToDefault' | transloco"
                  (clicked)="clearOverride(node.path)"
                >
                  <span class="material-symbols-outlined">close</span>
                </app-icon-button>
              }
            </div>
          }

          <!-- Number/integer (plain) -->
          @else if (node.type === 'number' || node.type === 'integer') {
            <app-input
              type="number"
              [value]="$any(getDisplayValue(node.path, node.defaultValue) ?? '')"
              (valueChange)="setSmartOverride(node.path, $event !== '' && $event != null ? +$event : null, node.defaultValue)"
            />
          }

          <!-- Boolean -->
          @else if (node.type === 'boolean') {
            <div class="toggle-field">
              <app-checkbox
                [checked]="$any(getDisplayValue(node.path, node.defaultValue) ?? false)"
                (changed)="onBooleanValueChange(node.path, $event, node.defaultValue)"
              />
              @if (isModified(node.path)) {
                <app-icon-button
                  variant="danger"
                  size="sm"
                  type="button"
                  [ariaLabel]="'configEditor.resetToDefault' | transloco"
                  [tooltip]="'configEditor.resetToDefault' | transloco"
                  (clicked)="clearOverride(node.path)"
                >
                  <span class="material-symbols-outlined">close</span>
                </app-icon-button>
              }
            </div>
          }

          <!-- Array of strings -->
          @else if (node.type === 'array') {
            <app-input
              type="text"
              [value]="getArrayDisplay(node.path, node.defaultValue)"
              (valueChange)="setArrayOverride(node.path, $event, node.defaultValue)"
            />
            <span class="field-hint">{{ 'configEditor.arrayHint' | transloco }}</span>
          }

          @if (node.description) {
            <div class="field-desc">{{ node.description }}</div>
          }
        </div>
      </ng-template>
    }
  `,
  styles: [`
    :host { display: block; }

    .loading {
      text-align: center;
      padding: 24px;
      color: var(--text-muted, #6c7086);
      font-size: 13px;
    }

    /* Mode toggle */
    .mode-toggle {
      display: flex;
      align-items: center;
      gap: 6px;
      margin-bottom: 12px;
    }

    .override-count {
      margin-left: auto;
      padding: 4px 10px;
      font-size: 11px;
      color: var(--text-muted, #6c7086);
    }

    .override-count.has-overrides {
      color: var(--accent-color, #cba6f7);
      font-weight: 600;
    }

    /* Sections */
    .config-section { margin-bottom: 6px; }

    .section-header {
      display: flex;
      align-items: center;
      gap: 8px;
      width: 100%;
      padding: 8px 12px;
      background: var(--surface-0, #313244);
      border: 1px solid var(--border-color, #313244);
      border-radius: 6px;
      cursor: pointer;
      color: var(--text-primary, #cdd6f4);
      font-size: 13px;
      font-weight: 600;
      font-family: inherit;
      transition: background 0.15s ease;
    }

    .section-header:hover { background: rgba(255, 255, 255, 0.05); }

    .toggle-icon {
      margin-left: auto;
      font-size: 18px;
      color: var(--text-muted, #6c7086);
    }

    .toggle-icon.sub { font-size: 16px; }

    .section-desc {
      padding: 4px 12px 0;
      font-size: 11px;
      color: var(--text-muted, #6c7086);
    }

    .section-body {
      padding: 12px 8px 4px;
    }

    /* Nested subsection */
    .config-subsection {
      margin-bottom: 8px;
      border-left: 2px solid var(--border-color, #313244);
      padding-left: 12px;
    }

    .subsection-header {
      display: flex;
      align-items: center;
      gap: 6px;
      width: 100%;
      padding: 6px 8px;
      background: transparent;
      border: none;
      cursor: pointer;
      color: var(--text-secondary, #a6adc8);
      font-size: 12px;
      font-weight: 600;
      font-family: inherit;
    }

    .subsection-header:hover { color: var(--text-primary, #cdd6f4); }

    .subsection-body { padding: 8px 0 4px; }

    /* Fields */
    .config-field {
      margin-bottom: 14px;
      padding: 0 4px;
    }

    .config-field.modified { }

    .field-label {
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 12px;
      font-weight: 500;
      color: var(--text-secondary, #a6adc8);
      margin-bottom: 4px;
    }

    .modified-dot {
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: var(--accent-color, #cba6f7);
      flex-shrink: 0;
    }

    .field-desc {
      font-size: 11px;
      color: var(--text-muted, #6c7086);
      margin-top: 3px;
      line-height: 1.4;
    }

    /* Slider */
    .slider-row {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .form-range {
      flex: 1;
      accent-color: var(--accent-color, #cba6f7);
      cursor: pointer;
    }

    .slider-value {
      min-width: 36px;
      text-align: right;
      font-size: 13px;
      font-weight: 600;
      color: var(--text-primary, #cdd6f4);
      font-family: 'JetBrains Mono', monospace;
    }

    /* Toggle / checkbox */
    .toggle-field {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .field-hint {
      font-size: 11px;
      color: var(--text-muted, #6c7086);
      margin-top: 2px;
    }

    /* JSON editor */
    app-textarea.json-editor {
      font-family: 'JetBrains Mono', monospace;
    }

    .json-error {
      font-size: 12px;
      color: var(--red, #f38ba8);
      margin-top: 4px;
      padding: 4px 8px;
      background: rgba(243, 139, 168, 0.08);
      border-radius: 4px;
    }
  `],
})
export class ConfigEditorComponent implements OnInit {
  private readonly http = inject(HttpClient);
  private readonly transloco = inject(TranslocoService);

  // Inputs
  readonly expertConfig = input<Record<string, unknown>>({});
  readonly value = input<Record<string, unknown>>({});

  // Outputs
  readonly valueChange = output<Record<string, unknown>>();

  // State
  readonly mode = signal<'visual' | 'json'>('visual');
  readonly schemaNodes = signal<SchemaNode[]>([]);
  readonly schemaLoaded = signal(false);
  readonly jsonText = signal('{}');
  readonly jsonError = signal<string | null>(null);
  readonly expandedSections = signal<Set<string>>(new Set(['llm']));
  readonly overrides = signal<Map<string, unknown>>(new Map());

  private schemaDefs: Record<string, any> = {};

  readonly overrideCount = () => this.overrides().size;

  constructor() {
    // Sync initial value input into internal overrides map
    effect(() => {
      const val = this.value();
      if (val && Object.keys(val).length > 0) {
        this.overrides.set(flattenObject(val));
      }
    });
  }

  ngOnInit(): void {
    this.http.get<any>('/assets/schema.json').subscribe({
      next: (schema) => {
        this.schemaDefs = schema.$defs ?? {};
        this.schemaNodes.set(parseSchema(schema, this.schemaDefs, '', SKIP_KEYS));
        this.schemaLoaded.set(true);
      },
      error: () => {
        this.schemaLoaded.set(true); // Show empty state
      },
    });
  }

  // ===== Section collapse =====

  toggleSection(path: string): void {
    const current = new Set(this.expandedSections());
    if (current.has(path)) {
      current.delete(path);
    } else {
      current.add(path);
    }
    this.expandedSections.set(current);
  }

  isSectionExpanded(path: string): boolean {
    return this.expandedSections().has(path);
  }

  sectionOverrideCount(sectionPath: string): number {
    let count = 0;
    const prefix = sectionPath + '.';
    this.overrides().forEach((_, key) => {
      if (key.startsWith(prefix)) count++;
    });
    return count;
  }

  // ===== Field value access =====

  /** Returns the effective value: override > expert config > schema default. */
  getDisplayValue(path: string, schemaDefault: unknown): unknown {
    const override = this.overrides().get(path);
    if (override !== undefined) return override;
    const expert = getNestedValue(this.expertConfig(), path);
    if (expert !== undefined && expert !== null) return expert;
    return schemaDefault ?? null;
  }

  getExpertValue(path: string): unknown {
    return getNestedValue(this.expertConfig(), path) ?? null;
  }

  /** Returns the baseline value (expert > schema default) that we compare against. */
  private getBaselineValue(path: string, schemaDefault: unknown): unknown {
    const expert = getNestedValue(this.expertConfig(), path);
    if (expert !== undefined && expert !== null) return expert;
    return schemaDefault ?? null;
  }

  isModified(path: string): boolean {
    return this.overrides().has(path);
  }

  /** Set override only if value differs from baseline (expert > schema default). */
  setSmartOverride(path: string, value: unknown, schemaDefault: unknown): void {
    const baseline = this.getBaselineValue(path, schemaDefault);
    const current = new Map(this.overrides());

    // eslint-disable-next-line eqeqeq -- intentional loose comparison for number/string coercion from form inputs
    if (value == baseline || (value === null && baseline === null)) {
      current.delete(path);
    } else if (value === null || value === undefined || value === '') {
      current.delete(path);
    } else {
      current.set(path, value);
    }

    this.overrides.set(current);
    this.emitValue();
  }

  clearOverride(path: string): void {
    const current = new Map(this.overrides());
    current.delete(path);
    this.overrides.set(current);
    this.emitValue();
  }

  onBooleanChange(path: string, event: Event, schemaDefault: unknown): void {
    const checked = (event.target as HTMLInputElement).checked;
    this.onBooleanValueChange(path, checked, schemaDefault);
  }

  onBooleanValueChange(path: string, checked: boolean, schemaDefault: unknown): void {
    const baseline = this.getBaselineValue(path, schemaDefault);

    if (checked === baseline) {
      this.clearOverride(path);
    } else {
      const current = new Map(this.overrides());
      current.set(path, checked);
      this.overrides.set(current);
      this.emitValue();
    }
  }

  asInputValue(event: Event): string {
    return (event.target as HTMLInputElement).value;
  }

  // ===== Array fields =====

  getArrayDisplay(path: string, schemaDefault: unknown): string {
    const override = this.overrides().get(path);
    if (override !== undefined && Array.isArray(override)) {
      return override.join(', ');
    }
    // Show expert or schema default
    const expert = getNestedValue(this.expertConfig(), path);
    if (Array.isArray(expert)) return expert.join(', ');
    if (Array.isArray(schemaDefault)) return (schemaDefault as string[]).join(', ');
    return '';
  }

  setArrayOverride(path: string, value: string, schemaDefault: unknown): void {
    const arr = value ? value.split(',').map((s) => s.trim()).filter(Boolean) : [];
    const baseline = this.getBaselineValue(path, schemaDefault);
    const baselineArr = Array.isArray(baseline) ? baseline : [];

    // Compare arrays — if same, remove override
    if (arr.length === baselineArr.length && arr.every((v, i) => v === baselineArr[i])) {
      this.clearOverride(path);
      return;
    }
    if (arr.length === 0 && baselineArr.length === 0) {
      this.clearOverride(path);
      return;
    }

    const current = new Map(this.overrides());
    current.set(path, arr);
    this.overrides.set(current);
    this.emitValue();
  }

  // ===== Mode switching =====

  switchMode(newMode: 'visual' | 'json'): void {
    if (newMode === this.mode()) return;

    if (newMode === 'json') {
      // Visual → JSON: show full resolved config (expert + overrides merged)
      const resolved = this.buildResolvedConfig();
      this.jsonText.set(JSON.stringify(resolved, null, 2));
      this.jsonError.set(null);
    } else {
      // JSON → Visual: diff the edited JSON against the expert config to extract overrides
      try {
        const edited = JSON.parse(this.jsonText());
        if (typeof edited === 'object' && edited !== null && !Array.isArray(edited)) {
          const expert = this.expertConfig();
          const diff = this.diffObjects(expert, edited);
          this.overrides.set(flattenObject(diff));
          this.jsonError.set(null);
          this.emitValue();
        } else {
          this.jsonError.set(this.transloco.translate('configEditor.errors.rootMustBeObject'));
          return;
        }
      } catch (e: any) {
        this.jsonError.set(e.message || this.transloco.translate('configEditor.errors.invalidJson'));
        return;
      }
    }

    this.mode.set(newMode);
  }

  onJsonEdit(text: string): void {
    this.jsonText.set(text);
    try {
      const edited = JSON.parse(text);
      if (typeof edited === 'object' && edited !== null && !Array.isArray(edited)) {
        this.jsonError.set(null);
        const expert = this.expertConfig();
        const diff = this.diffObjects(expert, edited);
        this.overrides.set(flattenObject(diff));
        this.emitValue();
      } else {
        this.jsonError.set(this.transloco.translate('configEditor.errors.rootMustBeObject'));
      }
    } catch (e: any) {
      this.jsonError.set(e.message || this.transloco.translate('configEditor.errors.invalidJson'));
    }
  }

  /** Build the full resolved config: expert config with overrides merged on top. */
  private buildResolvedConfig(): Record<string, unknown> {
    const expert = this.expertConfig();
    const overrideObj = buildNestedObject(this.overrides());
    return this.mergeDeep(expert, overrideObj);
  }

  /** Deep merge: source values override target. */
  private mergeDeep(target: Record<string, unknown>, source: Record<string, unknown>): Record<string, unknown> {
    const result = { ...target };
    for (const key of Object.keys(source)) {
      const sv = source[key];
      const tv = result[key];
      if (sv === null || sv === undefined) {
        delete result[key];
      } else if (
        typeof sv === 'object' && !Array.isArray(sv) &&
        typeof tv === 'object' && !Array.isArray(tv) && tv !== null
      ) {
        result[key] = this.mergeDeep(tv as Record<string, unknown>, sv as Record<string, unknown>);
      } else {
        result[key] = sv;
      }
    }
    return result;
  }

  /** Diff two objects: returns only the keys in `edited` that differ from `baseline`. */
  private diffObjects(baseline: Record<string, unknown>, edited: Record<string, unknown>): Record<string, unknown> {
    const diff: Record<string, unknown> = {};
    for (const key of Object.keys(edited)) {
      const bv = baseline[key];
      const ev = edited[key];
      if (
        typeof ev === 'object' && ev !== null && !Array.isArray(ev) &&
        typeof bv === 'object' && bv !== null && !Array.isArray(bv)
      ) {
        const nestedDiff = this.diffObjects(bv as Record<string, unknown>, ev as Record<string, unknown>);
        if (Object.keys(nestedDiff).length > 0) {
          diff[key] = nestedDiff;
        }
      } else if (JSON.stringify(ev) !== JSON.stringify(bv)) {
        diff[key] = ev;
      }
    }
    return diff;
  }

  // ===== Output =====

  private emitValue(): void {
    const obj = buildNestedObject(this.overrides());
    this.valueChange.emit(obj);
  }
}
