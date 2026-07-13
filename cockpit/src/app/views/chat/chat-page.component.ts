import {Component, DestroyRef, effect, inject, OnDestroy, OnInit} from '@angular/core';
import {takeUntilDestroyed} from '@angular/core/rxjs-interop';
import {ActivatedRoute, Router} from '@angular/router';
import {distinctUntilChanged, map} from 'rxjs';
import {PersistentChatComponent} from '../../views/persistent-chat/persistent-chat.component';
import {PersistentChatService} from '../../core/services/persistent-chat.service';
import {AppToastService} from '../../ui/toast';
import {ErrorMessageService} from '../../core/services/error-message.service';
import {CanvasService} from '../../core/services/canvas.service';

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
    private readonly canvas = inject(CanvasService);
    private readonly destroyRef = inject(DestroyRef);
    private routeGeneration = 0;

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
            this.canvas.selectThread(null);
            this.chat.enterDraftSession();
            return;
        }

        // Angular reuses this component for /sessions/:threadId → another
        // /sessions/:threadId navigation. Observe params for the component's
        // whole lifetime so both chat and Canvas switch together.
        this.route.paramMap.pipe(
            map(params => params.get('threadId')),
            distinctUntilChanged(),
            takeUntilDestroyed(this.destroyRef),
        ).subscribe(threadId => this.handleThreadRoute(threadId));
    }

    private handleThreadRoute(threadId: string | null): void {
        const routeGeneration = ++this.routeGeneration;

        if (threadId === '_creating') {
            this.canvas.selectThread(null);
            // Navigate arrived before thread exists — create it now
            const state = history.state as { createBody?: Record<string, any> };
            if (state?.createBody) {
                this.chat.createAndConnect(state.createBody).then(
                    id => {
                        if (routeGeneration !== this.routeGeneration) return false;
                        this.canvas.selectThread(id);
                        return this.router.navigate(['/sessions', id], {replaceUrl: true});
                    },
                    err => {
                        if (routeGeneration !== this.routeGeneration) return;
                        this.toast.danger(this.errors.translate(err, 'errors.sessions.createFailed'));
                        void this.router.navigate(['/sessions']);
                    }
                );
            } else {
                void this.router.navigate(['/sessions']);
            }
        } else if (threadId) {
            // Canvas state reconciles independently from chat history and may
            // remain available even when the live agent transport is offline.
            this.canvas.selectThread(threadId);
            // Already connected or mid-start on this thread? Don't reconnect.
            // The mid-start case is the draft flow landing here right after
            // createAndConnect — a second connect() would race the first.
            if (
                this.chat.threadId() === threadId &&
                (this.chat.isConnected() || this.chat.isStartingSession())
            ) return;

            void this.chat.connect(threadId);
        } else {
            this.canvas.selectThread(null);
            void this.router.navigate(['/sessions']);
        }
    }

    ngOnDestroy(): void {
        this.routeGeneration++;
        // Don't disconnect — keep session alive across navigation
    }
}
