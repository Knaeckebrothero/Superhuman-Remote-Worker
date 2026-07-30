import {ChangeDetectionStrategy, Component, OnInit, inject, signal} from '@angular/core';
import {FormsModule} from '@angular/forms';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';
import {forkJoin, of, switchMap} from 'rxjs';

import {Contact, ContactProjectRef} from '../../core/models/api.model';
import {ApiService} from '../../core/services/api.service';
import {ContactsService} from '../../core/services/contacts.service';
import {UserService} from '../../core/services/user.service';
import {AppConfirmNameDialogComponent} from '../../ui/confirm-name-dialog';
import {ContactFormComponent, ContactFormResult} from './contact-form.component';
import {ContactListComponent} from './contact-list.component';

@Component({
  selector: 'app-contacts-page',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormsModule, TranslocoPipe, ContactListComponent, ContactFormComponent,
            AppConfirmNameDialogComponent],
  template: `
    <div class="page-header">
      <h2>{{ 'contacts.title' | transloco }}</h2>
      <button [disabled]="showForm()" (click)="openNew()">{{ 'contacts.new' | transloco }}</button>
    </div>
    @if (saveError(); as err) {
      <div class="error-msg">
        <span>{{ err }}</span>
        <button (click)="saveError.set(null)">{{ 'common.dismiss' | transloco }}</button>
      </div>
    }
    <div class="filters">
      <input [ngModel]="q()" (ngModelChange)="q.set($event); reload()"
        [placeholder]="'contacts.search' | transloco" />
      <select [ngModel]="channel()" (ngModelChange)="channel.set($event); reload()">
        <option value="">{{ 'contacts.filter.allChannels' | transloco }}</option>
        <option value="email">email</option>
        <option value="whatsapp">whatsapp</option>
      </select>
      <select [ngModel]="projectId()" (ngModelChange)="projectId.set($event); reload()">
        <option value="">{{ 'contacts.filter.allProjects' | transloco }}</option>
        @for (p of projects(); track p.id) { <option [value]="p.id">{{ p.name }}</option> }
      </select>
    </div>
    @if (showForm()) {
      <app-contact-form [contact]="editing()" [projects]="projects()"
        (saved)="save($event)" (cancelled)="closeForm()" />
    }
    <app-contact-list [contacts]="contacts()" [currentUserId]="userService.currentUserId()"
      (edit)="openEdit($event)" (remove)="askDelete($event)" />
    @if (deleting(); as target) {
      <app-confirm-name-dialog
        [open]="!!deleting()"
        [title]="'contacts.delete.title' | transloco"
        [message]="deleteMessage(target)"
        [requiredName]="target.display_name"
        [namePrompt]="'contacts.delete.namePrompt' | transloco"
        [confirmLabel]="'common.delete' | transloco"
        [cancelLabel]="'common.cancel' | transloco"
        (confirmed)="doDelete(target)"
        (dismissed)="deleting.set(null)" />
    }
  `,
  styles: [`
    .page-header { display: flex; justify-content: space-between; align-items: center; }
    .filters { display: flex; gap: .5rem; margin: .5rem 0 1rem; }
    .error-msg { display: flex; align-items: center; justify-content: space-between;
      gap: .5rem; padding: .5rem .75rem; margin: .5rem 0; border-radius: 6px;
      background: var(--danger-tint); border: 1px solid var(--danger-tint); color: var(--danger); }
  `],
})
export class ContactsPageComponent implements OnInit {
  private readonly api = inject(ContactsService);
  private readonly projectsApi = inject(ApiService);   // loadProjects: house projects listing
  private readonly transloco = inject(TranslocoService);
  // Not private: read directly from the template ([currentUserId] binding
  // below), mirroring sidebar.component.ts's `readonly userService`.
  readonly userService = inject(UserService);

  readonly contacts = signal<Contact[]>([]);
  readonly projects = signal<ContactProjectRef[]>([]);
  readonly showForm = signal(false);
  readonly editing = signal<Contact | null>(null);
  readonly deleting = signal<Contact | null>(null);
  readonly saveError = signal<string | null>(null);
  readonly q = signal('');
  readonly channel = signal('');
  readonly projectId = signal('');

  ngOnInit(): void {
    this.reload();
    this.loadProjects();
  }

  reload(): void {
    this.api.list({q: this.q() || undefined, channel: this.channel() || undefined,
                   project_id: this.projectId() || undefined})
      .subscribe(rows => this.contacts.set(rows));
  }

  private loadProjects(): void {
    // House projects listing (ApiService.getProjects) — already catches errors
    // and resolves to [] on failure, so no separate error handler is needed here.
    this.projectsApi.getProjects().subscribe(rows => this.projects.set(rows));
  }

  openNew(): void { this.saveError.set(null); this.editing.set(null); this.showForm.set(true); }
  openEdit(c: Contact): void { this.saveError.set(null); this.editing.set(c); this.showForm.set(true); }
  closeForm(): void { this.saveError.set(null); this.showForm.set(false); this.editing.set(null); }

  deleteMessage(c: Contact): string {
    const names = c.projects.map(p => p.name).join(', ');
    return this.transloco.translate('contacts.delete.message', {projects: names || '—'});
  }

  askDelete(c: Contact): void { this.deleting.set(c); }

  doDelete(c: Contact): void {
    this.api.remove(c.id).subscribe({
      next: () => { this.deleting.set(null); this.reload(); },
      error: () => {
        // Without this handler a 403 (non-owner) or 404 (already deleted by
        // someone else) left `deleting()` non-null while the dialog had
        // already self-closed ([open]="!!deleting()" never changes value
        // again) — dead for the rest of the page's life. Clear it here so
        // the dialog can reopen, and surface the failure like save errors do.
        this.deleting.set(null);
        this.saveError.set(this.transloco.translate('contacts.deleteError'));
      },
    });
  }

  save(result: ContactFormResult): void {
    this.saveError.set(null);
    const existing = this.editing();
    const base$ = existing
      ? this.api.update(existing.id, {display_name: result.display_name, notes: result.notes})
      : this.api.create({display_name: result.display_name, notes: result.notes,
                         addresses: result.addresses});
    base$.pipe(switchMap(contact => {
      const ops = [];
      if (existing) {
        const beforeAddrs = existing.addresses;
        for (const a of beforeAddrs) {
          if (!result.addresses.some(r => r.id === a.id)) ops.push(this.api.removeAddress(a.id));
        }
        for (const r of result.addresses) {
          if (!r.id) { ops.push(this.api.addAddress(contact.id, r)); continue; }
          const before = beforeAddrs.find(a => a.id === r.id);
          if (before && (before.address !== r.address || before.is_primary !== r.is_primary)) {
            ops.push(this.api.patchAddress(r.id, {address: r.address, is_primary: r.is_primary}));
          }
        }
      }
      const beforeProjects = new Set((existing?.projects ?? []).map(p => p.id));
      for (const pid of result.projectIds) {
        if (!beforeProjects.has(pid)) ops.push(this.api.link(contact.id, pid));
      }
      for (const pid of beforeProjects) {
        if (!result.projectIds.includes(pid)) ops.push(this.api.unlink(contact.id, pid));
      }
      return ops.length ? forkJoin(ops) : of(null);
    })).subscribe({next: () => { this.closeForm(); this.reload(); },
                   error: () => {
                     // forkJoin fails fast: some ops may already have applied server-side.
                     // No rollback — surface the failure and reload to show real state;
                     // the (still-open) form keeps the user's input so they can retry.
                     this.saveError.set(this.transloco.translate('contacts.saveError'));
                     this.reload();
                   }});
  }
}
