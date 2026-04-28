import {Component, inject, OnDestroy, OnInit} from '@angular/core';
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

    ngOnInit(): void {
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
            // Already connected to this thread? Don't reconnect.
            if (this.chat.isConnected() && this.chat.threadId() === threadId) return;

            this.chat.connect(threadId);
        } else {
            this.router.navigate(['/sessions']);
        }
    }

    ngOnDestroy(): void {
        // Don't disconnect — keep session alive across navigation
    }
}
