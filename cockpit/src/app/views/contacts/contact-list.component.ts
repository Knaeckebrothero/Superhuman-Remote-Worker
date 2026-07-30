import {ChangeDetectionStrategy, Component, inject, input, output, signal} from '@angular/core';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';

import {Contact, ContactChannel} from '../../core/models/api.model';

@Component({
  selector: 'app-contact-list',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TranslocoPipe],
  template: `
    @for (contact of contacts(); track contact.id) {
      <div class="contact-row" (click)="toggle(contact.id)">
        <span class="caret">{{ isExpanded(contact.id) ? '▾' : '▸' }}</span>
        <span class="name">{{ contact.display_name }}</span>
        @for (ch of channelsOf(contact); track ch) {
          <span class="chip" [class.pending]="chipLabel(contact, ch) !== ch">
            {{ chipLabel(contact, ch) }}
          </span>
        }
        <span class="proj-count">{{ contact.projects.length }} {{ 'contacts.projectsShort' | transloco }}</span>
      </div>
      @if (isExpanded(contact.id)) {
        <div class="contact-detail">
          @for (a of contact.addresses; track a.id) {
            <div class="addr">
              <span class="chip">{{ a.channel }}</span>
              <span>{{ a.address }}</span>
              @if (a.is_primary) { <span class="tag">{{ 'contacts.primary' | transloco }}</span> }
              @if (a.opt_in_status !== 'opted_in') {
                <span class="tag pending">{{ 'contacts.optIn.' + a.opt_in_status | transloco }}</span>
              }
            </div>
          }
          @if (!contact.addresses.length) {
            <div class="addr muted">{{ 'contacts.noAddresses' | transloco }}</div>
          }
          <div class="projects">
            @for (p of contact.projects; track p.id) { <span class="chip">{{ p.name }}</span> }
          </div>
          @if (contact.notes) { <p class="notes">{{ contact.notes }}</p> }
          @if (canModify(contact, currentUserId())) {
            <div class="actions">
              <button (click)="edit.emit(contact); $event.stopPropagation()">{{ 'common.edit' | transloco }}</button>
              <button (click)="remove.emit(contact); $event.stopPropagation()">{{ 'common.delete' | transloco }}</button>
            </div>
          }
        </div>
      }
    }
    @if (!contacts().length) { <p class="muted">{{ 'contacts.empty' | transloco }}</p> }
  `,
  styles: [`
    .contact-row { display: flex; align-items: center; gap: .5rem; padding: .5rem .75rem;
      border-bottom: 1px solid var(--border-color, rgba(128,128,128,.25)); cursor: pointer; }
    .name { flex: 1; font-weight: 600; }
    .chip { border: 1px solid var(--border-color, rgba(128,128,128,.4)); border-radius: 999px;
      padding: 0 .5rem; font-size: .75rem; }
    .chip.pending, .tag.pending { border-color: var(--warning-color, #e6963c); }
    .contact-detail { padding: .5rem .75rem .75rem 2rem;
      border-bottom: 1px solid var(--border-color, rgba(128,128,128,.25)); }
    .addr { display: flex; gap: .5rem; align-items: center; margin: .25rem 0; }
    .muted { opacity: .6; }
    .actions { display: flex; gap: .5rem; margin-top: .5rem; }
  `],
})
export class ContactListComponent {
  readonly contacts = input<Contact[]>([]);
  /** Visibility is owned ∪ project-linked (spec), but only the owner may
   * mutate — co-members must see, not edit/delete, contacts they don't own. */
  readonly currentUserId = input<string | null>(null);
  readonly edit = output<Contact>();
  readonly remove = output<Contact>();

  private readonly transloco = inject(TranslocoService);
  private readonly expanded = signal<Set<string>>(new Set());

  isExpanded(id: string): boolean {
    return this.expanded().has(id);
  }

  toggle(id: string): void {
    const next = new Set(this.expanded());
    next.has(id) ? next.delete(id) : next.add(id);
    this.expanded.set(next);
  }

  channelsOf(contact: Contact): string[] {
    return [...new Set(contact.addresses.map(a => a.channel))].sort();
  }

  /** Chip carries opt-in state when the channel's primary isn't opted_in (spec). */
  chipLabel(contact: Contact, channel: ContactChannel | string): string {
    const primary = contact.addresses.find(a => a.channel === channel && a.is_primary)
      ?? contact.addresses.find(a => a.channel === channel);
    if (!primary || primary.opt_in_status === 'opted_in') return channel;
    return `${channel}·${this.transloco.translate('contacts.optIn.' + primary.opt_in_status)}`;
  }

  /** Owner-only mutation gate. Takes `currentUserId` explicitly (rather than
   * reading the `currentUserId` input internally) so it stays a pure,
   * directly-testable predicate — this repo's vitest harness has no way to
   * drive a signal input()'s value outside of ngtsc/TestBed compilation
   * (see contact-form.component.spec.ts for the documented gap). */
  canModify(contact: Contact, currentUserId: string | null): boolean {
    return contact.owner_user_id === currentUserId;
  }
}
