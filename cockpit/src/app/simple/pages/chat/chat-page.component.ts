import {Component, inject, OnDestroy, OnInit} from '@angular/core';
import {ActivatedRoute, Router} from '@angular/router';
import {PersistentChatComponent} from '../../../shared/components/persistent-chat/persistent-chat.component';
import {PersistentChatService} from '../../../core/services/persistent-chat.service';

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

    ngOnInit(): void {
        const threadId = this.route.snapshot.paramMap.get('threadId');

        if (threadId) {
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
