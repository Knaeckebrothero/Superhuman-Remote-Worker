import {Component, effect, inject, OnDestroy, OnInit} from '@angular/core';
import {ActivatedRoute, Router} from '@angular/router';
import {PersistentChatComponent} from '../../views/persistent-chat/persistent-chat.component';
import {PersistentChatService} from '../../core/services/persistent-chat.service';
import {AppToastService} from '../../ui/toast';
import {ErrorMessageService} from '../../core/services/error-message.service';

@Component({
    selector: 'app-chat-page',
    standalone: true,
    imports: [PersistentChatComponent],
    template: `<app-persistent-chat />`,
    styles: [
        `
      :host {
        display: block;
        height: 100%;
      }
    `,
    ],
})
export class ChatPageComponent implements OnInit, OnDestroy {
    private readonly route = inject(ActivatedRoute);
    private readonly router = inject(Router);
    private readonly chat = inject(PersistentChatService);
    private readonly toast = inject(AppToastService);
    private readonly errors = inject(ErrorMessageService);

    /** Instant-landing draft chat at `/` (route data, not a URL param). */
    private readonly isDraftRoute = this.route.snapshot.data['draft'] === true;

    constructor() {
        // Draft flow: when the first send creates the thread
        // (_createFromDraftSession → createAndConnect sets threadId), move the
        // URL from / to the session. No replaceUrl — Back returns to a fresh
        // draft. The destination ChatPage instance skips reconnecting via the
        // ngOnInit same-thread guard below.
        effect(() => {
            const id = this.chat.threadId();
            if (this.isDraftRoute && id) {
                void this.router.navigate(['/sessions', id]);
            }
        });
    }

    ngOnInit(): void {
        if (this.isDraftRoute) {
            this.chat.enterDraftSession();
            return;
        }

        const threadId = this.route.snapshot.paramMap.get('threadId');

        if (threadId === '_creating') {
            // Navigate arrived before thread exists — create it now
            const state = history.state as { createBody?: Record<string, any> };
            if (state?.createBody) {
                this.chat.createAndConnect(state.createBody).then(
                    id => this.router.navigate(['/sessions', id], {replaceUrl: true}),
                    err => {
                        this.toast.danger(this.errors.translate(err, 'errors.sessions.createFailed'));
                        this.router.navigate(['/sessions']);
                    }
                );
            } else {
                this.router.navigate(['/sessions']);
            }
        } else if (threadId) {
            // Already connected or mid-start on this thread? Don't reconnect.
            // The mid-start case is the draft flow landing here right after
            // createAndConnect — a second connect() would race the first.
            if (
                this.chat.threadId() === threadId &&
                (this.chat.isConnected() || this.chat.isStartingSession())
            ) return;

            this.chat.connect(threadId);
        } else {
            this.router.navigate(['/sessions']);
        }
    }

    ngOnDestroy(): void {
        // Don't disconnect — keep session alive across navigation
    }
}
