import {ChangeDetectionStrategy, Component, OnInit, computed, input, output, signal} from '@angular/core';
import {FormsModule} from '@angular/forms';
import {TranslocoPipe} from '@jsverse/transloco';

import {Contact, ContactProjectRef} from '../../core/models/api.model';

export interface ContactFormResult {
  display_name: string;
  /** Trimmed; "" means "clear notes" (backend maps "" → NULL). */
  notes: string;
  addresses: {id?: string; channel: string; address: string; is_primary: boolean}[];
  projectIds: string[];
}

@Component({
  selector: 'app-contact-form',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormsModule, TranslocoPipe],
  template: `
    <div class="form-panel">
      <h3>{{ (contact() ? 'contacts.form.editTitle' : 'contacts.form.newTitle') | transloco }}</h3>
      <input [(ngModel)]="name" [placeholder]="'contacts.form.name' | transloco" />
      <textarea [(ngModel)]="notes" rows="3"
        [placeholder]="'contacts.form.notes' | transloco"></textarea>
      <div class="label">{{ 'contacts.form.addresses' | transloco }}</div>
      @for (row of rows(); track $index; let i = $index) {
        <div class="addr-row">
          <select [ngModel]="row.channel" (ngModelChange)="patchRow(i, {channel: $event})">
            <option value="email">email</option>
            <option value="whatsapp">whatsapp</option>
          </select>
          <input [ngModel]="row.address" (ngModelChange)="patchRow(i, {address: $event})"
            [placeholder]="row.channel === 'whatsapp' ? '+4917012345678' : 'anna@acme.de'" />
          <label><input type="checkbox" [ngModel]="row.is_primary"
            (ngModelChange)="patchRow(i, {is_primary: $event})" />{{ 'contacts.primary' | transloco }}</label>
          <button (click)="dropRow(i)">✕</button>
        </div>
      }
      <button (click)="addRow()">{{ 'contacts.form.addAddress' | transloco }}</button>
      <div class="label">{{ 'contacts.form.projects' | transloco }}</div>
      @for (p of projects(); track p.id) {
        <label class="proj">
          <input type="checkbox" [ngModel]="selectedProjects().has(p.id)"
            (ngModelChange)="toggleProject(p.id)" /> {{ p.name }}
        </label>
      }
      <div class="actions">
        <button (click)="cancelled.emit()">{{ 'common.cancel' | transloco }}</button>
        <button [disabled]="!valid()" (click)="submit()">{{ 'common.save' | transloco }}</button>
      </div>
    </div>
  `,
  styles: [`
    .form-panel { border: 1px solid var(--border-color, rgba(128,128,128,.4));
      border-radius: 8px; padding: 1rem; margin: .75rem 0; display: flex;
      flex-direction: column; gap: .5rem; }
    .addr-row { display: flex; gap: .5rem; align-items: center; }
    .label { font-size: .75rem; text-transform: uppercase; opacity: .7; margin-top: .5rem; }
    .actions { display: flex; justify-content: flex-end; gap: .5rem; }
  `],
})
export class ContactFormComponent implements OnInit {
  readonly contact = input<Contact | null>(null);
  readonly projects = input<ContactProjectRef[]>([]);
  readonly saved = output<ContactFormResult>();
  readonly cancelled = output<void>();

  name = '';
  notes = '';
  readonly rows = signal<{id?: string; channel: string; address: string; is_primary: boolean}[]>([]);
  readonly selectedProjects = signal<Set<string>>(new Set());
  readonly valid = computed(() =>
    this.name.trim().length > 0 || (this.contact()?.display_name ?? '').length > 0);

  ngOnInit(): void {
    const c = this.contact();
    if (c) {
      this.name = c.display_name;
      this.notes = c.notes ?? '';
      this.rows.set(c.addresses.map(a => ({
        id: a.id, channel: a.channel, address: a.address, is_primary: a.is_primary})));
      this.selectedProjects.set(new Set(c.projects.map(p => p.id)));
    }
  }

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
      display_name: this.name.trim(),
      notes: this.notes.trim(),
      addresses: this.rows().filter(r => r.address.trim()),
      projectIds: [...this.selectedProjects()],
    });
  }
}
