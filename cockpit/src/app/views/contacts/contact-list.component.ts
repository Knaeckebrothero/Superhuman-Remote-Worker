import {ChangeDetectionStrategy, Component, inject, input, output, signal} from '@angular/core';
import {TranslocoPipe, TranslocoService} from '@jsverse/transloco';

import {Contact, ContactAddress, ContactChannel} from '../../core/models/api.model';
import {AppBadgeComponent, BadgeTone} from '../../ui/badge';
import {AppButtonComponent} from '../../ui/button';
import {AppChipComponent} from '../../ui/chip';
import {AppIconComponent} from '../../ui/icon';

@Component({
  selector: 'app-contact-list',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TranslocoPipe, AppBadgeComponent, AppButtonComponent, AppChipComponent, AppIconComponent],
  template: `
    @for (contact of contacts(); track contact.id) {
      <div class="row" [class.row--open]="isExpanded(contact.id)">
        <button
          type="button"
          class="row__summary"
          [attr.aria-expanded]="isExpanded(contact.id)"
          (click)="toggle(contact.id)"
        >
          <app-icon size="sm" class="row__caret">
            {{ isExpanded(contact.id) ? 'expand_more' : 'chevron_right' }}
          </app-icon>
          <span class="row__monogram" aria-hidden="true">{{ initials(contact) }}</span>
          <span class="row__name">{{ contact.display_name }}</span>
          <span class="row__channels">
            @for (ch of channelsOf(contact); track ch) {
              <app-badge [tone]="chipTone(contact, ch)" size="xs">{{ chipLabel(contact, ch) }}</app-badge>
            }
            @if (!contact.addresses.length) {
              <app-badge tone="neutral" size="xs">{{ 'contacts.noAddresses' | transloco }}</app-badge>
            }
          </span>
          <span class="row__projects">
            {{ contact.projects.length }} {{ 'contacts.projectsShort' | transloco }}
          </span>
        </button>

        @if (isExpanded(contact.id)) {
          <div class="detail">
            <dl class="detail__addresses">
              @for (a of contact.addresses; track a.id) {
                <div class="addr">
                  <dt><app-badge [tone]="addressTone(a)" size="xs">{{ a.channel }}</app-badge></dt>
                  <dd>
                    <span class="addr__value">{{ a.address }}</span>
                    @if (a.is_primary) {
                      <span class="addr__note">{{ 'contacts.primary' | transloco }}</span>
                    }
                    @if (a.opt_in_status !== 'opted_in') {
                      <span class="addr__note addr__note--warn">
                        {{ 'contacts.optIn.' + a.opt_in_status | transloco }}
                      </span>
                    }
                  </dd>
                </div>
              }
              @if (!contact.addresses.length) {
                <p class="detail__hint">{{ 'contacts.noAddressesHint' | transloco }}</p>
              }
            </dl>

            @if (contact.projects.length) {
              <div class="detail__projects">
                @for (p of contact.projects; track p.id) {
                  <app-chip [selectable]="false" size="sm">{{ p.name }}</app-chip>
                }
              </div>
            }

            @if (contact.notes) {
              <p class="detail__notes">{{ contact.notes }}</p>
            }

            @if (canModify(contact, currentUserId())) {
              <div class="detail__actions">
                <app-button variant="secondary" size="sm" (clicked)="edit.emit(contact)">
                  {{ 'common.edit' | transloco }}
                </app-button>
                <app-button variant="danger" size="sm" (clicked)="remove.emit(contact)">
                  {{ 'common.delete' | transloco }}
                </app-button>
              </div>
            }
          </div>
        }
      </div>
    }

    @if (!contacts().length) {
      <div class="empty">
        <app-icon size="lg" class="empty__icon">group</app-icon>
        <p class="empty__title">{{ 'contacts.emptyTitle' | transloco }}</p>
        <p class="empty__body">{{ 'contacts.empty' | transloco }}</p>
      </div>
    }
  `,
  styles: [
    `
      :host {
        display: block;
      }

      .row {
        border: 1px solid var(--border-color);
        border-radius: var(--radius-surface);
        background: var(--surface-1);
        margin-bottom: 8px;
        overflow: hidden;
        transition: border-color 0.15s ease;
      }

      .row:hover {
        border-color: var(--accent-color);
      }

      .row--open {
        border-color: var(--accent-color);
      }

      /* A button, not a div: the row is the disclosure control, so it must be
         keyboard-reachable and announce its expanded state. Reset the chrome. */
      .row__summary {
        display: flex;
        align-items: center;
        gap: 10px;
        width: 100%;
        padding: 10px 12px;
        background: none;
        border: 0;
        font: inherit;
        color: inherit;
        text-align: left;
        cursor: pointer;
      }

      .row__summary:focus-visible {
        outline: 2px solid var(--accent-color);
        outline-offset: -2px;
      }

      .row__caret {
        color: var(--text-muted);
        flex-shrink: 0;
      }

      .row__monogram {
        flex-shrink: 0;
        width: 28px;
        height: 28px;
        border-radius: var(--radius-full);
        background: var(--surface-2);
        color: var(--text-secondary);
        display: grid;
        place-items: center;
        font-size: 0.72rem;
        font-weight: 600;
        letter-spacing: 0.02em;
      }

      .row__name {
        flex: 1;
        min-width: 0;
        font-weight: 600;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .row__channels {
        display: flex;
        gap: 4px;
        flex-wrap: wrap;
        justify-content: flex-end;
      }

      .row__projects {
        flex-shrink: 0;
        min-width: 4.5em;
        text-align: right;
        color: var(--text-muted);
        font-size: 0.78rem;
      }

      .detail {
        padding: 4px 12px 12px 50px;
        border-top: 1px solid var(--border-color);
      }

      .detail__addresses {
        margin: 8px 0 0;
      }

      .addr {
        display: flex;
        align-items: baseline;
        gap: 8px;
        margin: 0 0 4px;
      }

      .addr dt,
      .addr dd {
        margin: 0;
      }

      .addr dt {
        flex-shrink: 0;
        min-width: 5.5em;
      }

      .addr__value {
        font-family: var(--font-mono);
        font-size: 0.82rem;
        color: var(--text-primary);
      }

      .addr__note {
        margin-left: 8px;
        font-size: 0.72rem;
        color: var(--text-muted);
      }

      .addr__note--warn {
        color: var(--warning);
      }

      .detail__hint,
      .detail__notes {
        margin: 8px 0 0;
        color: var(--text-secondary);
        font-size: 0.85rem;
      }

      .detail__projects {
        display: flex;
        flex-wrap: wrap;
        gap: 4px;
        margin-top: 10px;
      }

      .detail__actions {
        display: flex;
        gap: 8px;
        margin-top: 12px;
      }

      .empty {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 4px;
        padding: 48px 16px;
        text-align: center;
      }

      .empty__icon {
        color: var(--text-muted);
        opacity: 0.5;
      }

      .empty__title {
        margin: 8px 0 0;
        font-weight: 600;
      }

      .empty__body {
        margin: 0;
        max-width: 42ch;
        color: var(--text-muted);
        font-size: 0.88rem;
      }

      @media (max-width: 640px) {
        .row__projects {
          display: none;
        }

        .detail {
          padding-left: 12px;
        }
      }
    `,
  ],
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
    const primary = this.primaryFor(contact, channel);
    if (!primary || primary.opt_in_status === 'opted_in') return channel;
    return `${channel}·${this.transloco.translate('contacts.optIn.' + primary.opt_in_status)}`;
  }

  /**
   * Badge tone answers the question the row exists to answer: can the agent
   * actually reach this person on this channel *right now*? Opt-in state is
   * the only thing that decides it, so it drives the colour rather than the
   * channel does — a pending WhatsApp number looks reachable but isn't.
   */
  chipTone(contact: Contact, channel: ContactChannel | string): BadgeTone {
    const primary = this.primaryFor(contact, channel);
    return primary ? this.addressTone(primary) : 'neutral';
  }

  addressTone(address: ContactAddress): BadgeTone {
    if (address.opt_in_status === 'opted_out') return 'danger';
    if (address.opt_in_status === 'pending') return 'warning';
    return 'neutral';
  }

  initials(contact: Contact): string {
    return (
      contact.display_name
        .split(/\s+/)
        .filter(Boolean)
        .slice(0, 2)
        .map(part => [...part][0] ?? '')
        .join('')
        .toUpperCase() || '?'
    );
  }

  /** Owner-only mutation gate. Takes `currentUserId` explicitly (rather than
   * reading the `currentUserId` input internally) so it stays a pure,
   * directly-testable predicate — this repo's vitest harness has no way to
   * drive a signal input()'s value outside of ngtsc/TestBed compilation
   * (see contact-form.component.spec.ts for the documented gap). */
  canModify(contact: Contact, currentUserId: string | null): boolean {
    return contact.owner_user_id === currentUserId;
  }

  private primaryFor(contact: Contact, channel: string): ContactAddress | undefined {
    return (
      contact.addresses.find(a => a.channel === channel && a.is_primary) ??
      contact.addresses.find(a => a.channel === channel)
    );
  }
}
