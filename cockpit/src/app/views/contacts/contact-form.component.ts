import {ChangeDetectionStrategy, Component, computed, input, linkedSignal, output} from '@angular/core';
import {TranslocoPipe} from '@jsverse/transloco';

import {Contact, ContactProjectRef} from '../../core/models/api.model';
import {AppButtonComponent} from '../../ui/button';
import {AppCheckboxComponent} from '../../ui/checkbox';
import {AppChipComponent} from '../../ui/chip';
import {AppFormFieldComponent} from '../../ui/form-field';
import {AppIconButtonComponent} from '../../ui/icon-button';
import {AppIconComponent} from '../../ui/icon';
import {AppInputComponent} from '../../ui/input';
import {AppSelectComponent} from '../../ui/select';
import {AppTextareaComponent} from '../../ui/textarea';

export interface ContactFormResult {
  display_name: string;
  /** Trimmed; "" means "clear notes" (backend maps "" → NULL). */
  notes: string;
  addresses: {id?: string; channel: string; address: string; is_primary: boolean}[];
  projectIds: string[];
}

// Pure seed functions, one per linkedSignal below. Exported (rather than
// inlined as lambdas) so contact-form.component.spec.ts can verify the
// re-seed-on-target-change contract by driving these with a plain signal()
// standing in for `contact()`: this Angular/vitest setup doesn't run
// components through ngtsc, so signal `input()` fields carry no compiled
// metadata under TestBed (`ɵcmp.inputs` is empty; both template binding and
// componentRef.setInput() reject 'contact'/'projects' as unrecognized —
// confirmed empirically, see the spec file) — there's currently no way to
// drive an input()'s value from a test in this repo. signal()/linkedSignal()
// carry no such requirement, so testing the derivation through them exactly
// mirrors what the linkedSignals below do with `this.contact()`.
export function seedName(contact: Contact | null): string {
  return contact?.display_name ?? '';
}
export function seedNotes(contact: Contact | null): string {
  return contact?.notes ?? '';
}
export function seedRows(contact: Contact | null):
    {id?: string; channel: string; address: string; is_primary: boolean}[] {
  return (contact?.addresses ?? []).map(a => (
    {id: a.id, channel: a.channel, address: a.address, is_primary: a.is_primary}));
}
export function seedProjectIds(contact: Contact | null): Set<string> {
  return new Set((contact?.projects ?? []).map(p => p.id));
}

@Component({
  selector: 'app-contact-form',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    TranslocoPipe,
    AppButtonComponent,
    AppCheckboxComponent,
    AppChipComponent,
    AppFormFieldComponent,
    AppIconButtonComponent,
    AppIconComponent,
    AppInputComponent,
    AppSelectComponent,
    AppTextareaComponent,
  ],
  template: `
    <div class="panel">
      <h3 class="panel__title">
        {{ (contact() ? 'contacts.form.editTitle' : 'contacts.form.newTitle') | transloco }}
      </h3>

      <app-form-field [label]="'contacts.form.name' | transloco" [required]="true">
        <app-input
          [value]="name()"
          (valueChange)="name.set($event)"
          [placeholder]="'contacts.form.namePlaceholder' | transloco"
        />
      </app-form-field>

      <app-form-field
        [label]="'contacts.form.notesLabel' | transloco"
        [hint]="'contacts.form.notes' | transloco"
      >
        <app-textarea [value]="notes()" (valueChange)="notes.set($event)" [rows]="3" />
      </app-form-field>

      <app-form-field
        [label]="'contacts.form.addresses' | transloco"
        [hint]="'contacts.form.addressesHint' | transloco"
      >
        @for (row of rows(); track $index; let i = $index) {
          <div class="addr">
            <app-select
              class="addr__channel"
              [value]="row.channel"
              (valueChange)="patchRow(i, {channel: $event ?? 'email'})"
              [disabled]="!!row.id"
              [fullWidth]="false"
              [ariaLabel]="'contacts.form.channelAria' | transloco"
            >
              <option value="email">email</option>
              <option value="whatsapp">whatsapp</option>
            </app-select>

            <app-input
              class="addr__value"
              [value]="row.address"
              (valueChange)="patchRow(i, {address: $event})"
              [placeholder]="row.channel === 'whatsapp' ? '+4917012345678' : 'anna@acme.de'"
              [ariaLabel]="'contacts.form.addressAria' | transloco"
            />

            <app-checkbox
              [checked]="row.is_primary"
              (checkedChange)="patchRow(i, {is_primary: $event})"
              [ariaLabel]="'contacts.primary' | transloco"
            >{{ 'contacts.primary' | transloco }}</app-checkbox>

            <app-icon-button
              variant="ghost"
              size="sm"
              [tooltip]="'contacts.form.removeAddress' | transloco"
              [ariaLabel]="'contacts.form.removeAddress' | transloco"
              (clicked)="dropRow(i)"
            ><app-icon size="sm">close</app-icon></app-icon-button>
          </div>

          @if (row.id) {
            <p class="addr__locked">
              <app-icon size="sm">lock</app-icon>
              {{ 'contacts.form.channelLocked' | transloco }}
            </p>
          }
        }

        <app-button variant="secondary" size="sm" (clicked)="addRow()">
          {{ 'contacts.form.addAddress' | transloco }}
        </app-button>
      </app-form-field>

      <app-form-field
        [label]="'contacts.form.projects' | transloco"
        [hint]="'contacts.form.projectsHint' | transloco"
      >
        @if (projects().length) {
          <div class="projects">
            @for (p of projects(); track p.id) {
              <app-chip
                [selected]="selectedProjects().has(p.id)"
                (clicked)="toggleProject(p.id)"
              >{{ p.name }}</app-chip>
            }
          </div>
        } @else {
          <p class="projects__empty">{{ 'contacts.form.noProjects' | transloco }}</p>
        }
      </app-form-field>

      <div class="panel__actions">
        <app-button variant="secondary" (clicked)="cancelled.emit()">
          {{ 'common.cancel' | transloco }}
        </app-button>
        <app-button variant="primary" [disabled]="!valid()" (clicked)="submit()">
          {{ 'common.save' | transloco }}
        </app-button>
      </div>
    </div>
  `,
  styles: [
    `
      .panel {
        border: 1px solid var(--border-color);
        border-radius: var(--radius-surface);
        background: var(--surface-1);
        padding: 16px;
        margin-bottom: 12px;
        display: flex;
        flex-direction: column;
        gap: 12px;
      }

      .panel__title {
        margin: 0;
        font-size: 0.95rem;
        font-weight: 600;
      }

      .addr {
        display: flex;
        align-items: center;
        gap: 8px;
        margin-bottom: 8px;
      }

      .addr__channel {
        flex: 0 0 8.5rem;
      }

      .addr__value {
        flex: 1;
        min-width: 0;
      }

      .addr__locked {
        display: flex;
        align-items: center;
        gap: 4px;
        margin: -4px 0 10px;
        color: var(--text-muted);
        font-size: 0.72rem;
      }

      .projects {
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
      }

      .projects__empty {
        margin: 0;
        color: var(--text-muted);
        font-size: 0.85rem;
      }

      .panel__actions {
        display: flex;
        justify-content: flex-end;
        gap: 8px;
        margin-top: 4px;
      }

      @media (max-width: 640px) {
        .addr {
          flex-wrap: wrap;
        }

        .addr__channel {
          flex: 0 0 auto;
        }
      }
    `,
  ],
})
export class ContactFormComponent {
  readonly contact = input<Contact | null>(null);
  readonly projects = input<ContactProjectRef[]>([]);
  readonly saved = output<ContactFormResult>();
  readonly cancelled = output<void>();

  /**
   * Local editable state derives from `contact()` via linkedSignal: it seeds
   * on first read AND re-seeds whenever `contact()` changes identity (a new
   * edit target, or edit->new), while staying freely writable for
   * in-progress edits in between. This replaces a one-shot `ngOnInit` seed,
   * which went stale when the page swapped edit targets without destroying
   * this component instance (`@if (showForm())` in contacts-page doesn't
   * re-toggle just because `editing()` changes) — the stale local state was
   * then saved over whichever contact was actually open, corrupting it.
   */
  readonly name = linkedSignal(() => seedName(this.contact()));
  readonly notes = linkedSignal(() => seedNotes(this.contact()));
  readonly rows = linkedSignal(() => seedRows(this.contact()));
  readonly selectedProjects = linkedSignal(() => seedProjectIds(this.contact()));

  /** A blank name is never submittable, editing or not. The earlier
   * `|| contact()?.display_name` fallback kept Save enabled while editing any
   * named contact, so clearing the field and saving sent `display_name: ""` —
   * now a 400 server-side, but the button should not offer it at all. */
  readonly valid = computed(() => this.name().trim().length > 0);

  addRow(): void {
    this.rows.update(r => [...r, {channel: 'email', address: '', is_primary: false}]);
  }
  dropRow(i: number): void {
    this.rows.update(r => r.filter((_, idx) => idx !== i));
  }
  patchRow(i: number, patch: Partial<{channel: string; address: string; is_primary: boolean}>): void {
    this.rows.update(r => r.map((row, idx) => (idx === i ? {...row, ...patch} : row)));
  }
  toggleProject(id: string): void {
    const next = new Set(this.selectedProjects());
    next.has(id) ? next.delete(id) : next.add(id);
    this.selectedProjects.set(next);
  }
  submit(): void {
    this.saved.emit({
      display_name: this.name().trim(),
      notes: this.notes().trim(),
      addresses: this.rows().filter(r => r.address.trim()),
      projectIds: [...this.selectedProjects()],
    });
  }
}
