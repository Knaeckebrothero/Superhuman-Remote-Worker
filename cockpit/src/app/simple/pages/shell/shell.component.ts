import {Component, ElementRef, HostListener, inject, OnInit, signal, ViewChild} from '@angular/core';
import {TranslocoDatePipe} from '@jsverse/transloco-locale';
import {JobArtifactService} from '../../../core/services/job-artifact.service';
import {ModelService} from '../../../core/services/model.service';
import {UserService} from '../../../core/services/user.service';
import {BuilderSession, BuilderStreamService} from '../../../core/services/builder-stream.service';
import {SidebarToggleComponent} from '../../layout/sidebar-toggle/sidebar-toggle.component';
import {
    InstructionBuilderComponent
} from '../../../shared/components/instruction-builder/instruction-builder.component';
import {TranslocoPipe} from '@jsverse/transloco';

@Component({
  selector: 'app-shell-page',
  standalone: true,
  imports: [SidebarToggleComponent, InstructionBuilderComponent, TranslocoDatePipe, TranslocoPipe],
  template: `
      <div class="page">
        <header class="page-header">
          <app-sidebar-toggle />

          <div class="session-controls">
            <button class="session-title-btn" (click)="toggleSessionDropdown()">
              <span class="session-title-text">{{ artifacts.sessionTitle() || ('shell.newSession' | transloco) }}</span>
              <span class="dropdown-arrow">expand_more</span>
            </button>

            @if (showSessionDropdown()) {
              <div class="session-dropdown">
                <button class="session-option new-option" (click)="startNewSession()">
                  <span class="new-icon">add</span>
                  <span>{{ 'shell.newSession' | transloco }}</span>
                </button>
                @for (s of sessions(); track s.id) {
                  <button
                    class="session-option"
                    [class.active]="s.id === artifacts.sessionId()"
                    (click)="switchSession(s.id, s.title)"
                  >
                    <span class="session-option-title">{{ s.title || ('shell.untitledSession' | transloco) }}</span>
                    <span class="session-option-date">{{ s.updated_at | translocoDate:{dateStyle:'short', timeStyle:'short'} }}</span>
                  </button>
                }
              </div>
            }
          </div>

          <div class="header-spacer"></div>

          <div class="model-controls">
            <button class="model-title-btn" (click)="toggleModelDropdown()">
              <span class="model-title-text">{{ artifacts.builderModel() || ('shell.selectModel' | transloco) }}</span>
              <span class="dropdown-arrow">expand_more</span>
            </button>

            @if (showModelDropdown()) {
              <div class="model-dropdown">
                @for (group of modelGroups(); track group.group) {
                  <div class="model-group-header" [class.unconfigured]="!group.configured">
                    {{ group.configured ? group.group : group.group + ' ' + ('shell.noKey' | transloco) }}
                  </div>
                  @for (model of group.models; track model) {
                    <button
                      class="model-option"
                      [class.active]="model === artifacts.builderModel()"
                      [class.unconfigured]="!group.configured"
                      (click)="selectModel(model)"
                    >{{ model }}</button>
                  }
                }
              </div>
            }
          </div>

          @if (artifacts.streaming()) {
            <span class="streaming-badge">
              <span class="pulse-dot"></span>
            </span>
          }
        </header>
        <main class="page-content">
          <app-instruction-builder />
        </main>
      </div>
  `,
  styles: [
    `
      :host {
        display: block;
        height: 100%;
      }

      .page {
        display: flex;
        flex-direction: column;
        height: 100%;
      }

      .page-header {
        display: flex;
        align-items: center;
        gap: 8px;
        height: 48px;
        padding: 0 12px;
        background: var(--timeline-bg, #11111b);
        border-bottom: 1px solid var(--border-color, #313244);
        flex-shrink: 0;
      }

      .header-spacer {
        flex: 1;
      }

      /* Session controls */
      .session-controls {
        position: relative;
      }

      .session-title-btn {
        display: flex;
        align-items: center;
        gap: 4px;
        padding: 6px 10px;
        border: 1px solid var(--border-color, #313244);
        border-radius: 6px;
        background: transparent;
        color: var(--text-primary, #cdd6f4);
        font-size: 12px;
        font-weight: 500;
        cursor: pointer;
        max-width: 240px;
        min-height: 36px;
      }

      .session-title-btn:hover {
        background: rgba(255, 255, 255, 0.04);
        border-color: var(--accent-color, #cba6f7);
      }

      .session-title-text {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .dropdown-arrow {
        font-family: 'Material Symbols Outlined';
        font-size: 18px;
        color: var(--text-muted, #6c7086);
        flex-shrink: 0;
      }

      .session-dropdown {
        position: absolute;
        top: calc(100% + 4px);
        left: 0;
        min-width: 280px;
        max-height: 300px;
        overflow-y: auto;
        background: var(--surface-0, #313244);
        border: 1px solid var(--border-color, #45475a);
        border-radius: 8px;
        box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
        z-index: 100;
      }

      .session-option {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        width: 100%;
        padding: 8px 12px;
        border: none;
        background: transparent;
        color: var(--text-primary, #cdd6f4);
        font-size: 12px;
        cursor: pointer;
        text-align: left;
      }

      .session-option:hover {
        background: rgba(255, 255, 255, 0.06);
      }

      .session-option.active {
        background: rgba(203, 166, 247, 0.12);
      }

      .new-option {
        border-bottom: 1px solid var(--border-color, #45475a);
        color: var(--accent-color, #cba6f7);
        font-weight: 500;
        gap: 6px;
        justify-content: flex-start;
      }

      .new-icon {
        font-family: 'Material Symbols Outlined';
        font-size: 16px;
      }

      .session-option-title {
        flex: 1;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .session-option-date {
        font-size: 10px;
        color: var(--text-muted, #6c7086);
        flex-shrink: 0;
      }

      /* Model controls */
      .model-controls {
        position: relative;
      }

      .model-title-btn {
        display: flex;
        align-items: center;
        gap: 4px;
        padding: 6px 10px;
        border: 1px solid var(--border-color, #313244);
        border-radius: 6px;
        background: transparent;
        color: var(--text-primary, #cdd6f4);
        font-size: 12px;
        font-weight: 500;
        cursor: pointer;
        max-width: 260px;
        min-height: 36px;
      }

      .model-title-btn:hover {
        background: rgba(255, 255, 255, 0.04);
        border-color: var(--accent-color, #cba6f7);
      }

      .model-title-text {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .model-dropdown {
        position: absolute;
        top: calc(100% + 4px);
        right: 0;
        min-width: 280px;
        max-height: 360px;
        overflow-y: auto;
        background: var(--surface-0, #313244);
        border: 1px solid var(--border-color, #45475a);
        border-radius: 8px;
        box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
        z-index: 100;
      }

      .model-group-header {
        padding: 6px 12px 4px;
        font-size: 10px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        color: var(--text-muted, #6c7086);
        border-top: 1px solid var(--border-color, #45475a);
      }

      .model-group-header:first-child {
        border-top: none;
      }

      .model-group-header.unconfigured {
        opacity: 0.5;
      }

      .model-option {
        display: block;
        width: 100%;
        padding: 7px 12px 7px 20px;
        border: none;
        background: transparent;
        color: var(--text-primary, #cdd6f4);
        font-size: 12px;
        cursor: pointer;
        text-align: left;
      }

      .model-option:hover {
        background: rgba(255, 255, 255, 0.06);
      }

      .model-option.active {
        background: rgba(203, 166, 247, 0.12);
        color: var(--accent-color, #cba6f7);
      }

      .model-option.unconfigured {
        opacity: 0.5;
      }

      .streaming-badge {
        display: flex;
        align-items: center;
        flex-shrink: 0;
      }

      .pulse-dot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background: var(--accent-color, #cba6f7);
        animation: pulse 1.5s ease-in-out infinite;
      }

      @keyframes pulse {
        0%,
        100% {
          opacity: 1;
        }
        50% {
          opacity: 0.3;
        }
      }

      .page-content {
        flex: 1;
        overflow: hidden;
      }

      @media (max-width: 768px) {
        .page-header {
          gap: 4px;
          padding: 0 8px;
          height: 44px;
        }

        .session-title-btn,
        .model-title-btn {
          font-size: 11px;
          padding: 4px 8px;
          min-height: 32px;
        }

        .session-title-btn {
          max-width: 140px;
        }

        .model-title-btn {
          max-width: 120px;
        }

        .session-dropdown,
        .model-dropdown {
          min-width: unset;
          width: calc(100vw - 16px);
          max-height: 50vh;
          border-radius: 10px;
        }
      }
    `,
  ],
})
export class ShellPageComponent implements OnInit {
  private readonly el = inject(ElementRef);
  readonly artifacts = inject(JobArtifactService);
  private readonly modelService = inject(ModelService);
  private readonly userService = inject(UserService);
  private readonly stream = inject(BuilderStreamService);
  readonly modelGroups = this.modelService.models;

  readonly sessions = signal<BuilderSession[]>([]);
  readonly showSessionDropdown = signal(false);
  readonly showModelDropdown = signal(false);

  @ViewChild(InstructionBuilderComponent) builder?: InstructionBuilderComponent;

  @HostListener('document:click', ['$event'])
  onDocumentClick(event: MouseEvent): void {
    const target = event.target as Node;
    if (this.showSessionDropdown()) {
      const controls = this.el.nativeElement.querySelector('.session-controls');
      if (controls && !controls.contains(target)) {
        this.showSessionDropdown.set(false);
      }
    }
    if (this.showModelDropdown()) {
      const modelControls = this.el.nativeElement.querySelector('.model-controls');
      if (modelControls && !modelControls.contains(target)) {
        this.showModelDropdown.set(false);
      }
    }
  }

  ngOnInit(): void {
    this.modelService.load();
  }

  toggleModelDropdown(): void {
    this.showModelDropdown.update((v) => !v);
  }

  selectModel(model: string): void {
    this.showModelDropdown.set(false);
    this.artifacts.persistBuilderModel(model);
  }

  toggleSessionDropdown(): void {
    const next = !this.showSessionDropdown();
    if (next) {
      this.loadSessions();
    }
    this.showSessionDropdown.set(next);
  }

  startNewSession(): void {
    this.showSessionDropdown.set(false);
    this.builder?.startNewSession();
  }

  switchSession(sessionId: string, title: string | null): void {
    this.showSessionDropdown.set(false);
    this.builder?.switchSession(sessionId, title);
  }

  private loadSessions(): void {
    const userId = this.userService.currentUserId();
    if (!userId) return;
    this.stream.listSessions(userId).subscribe((sessions) => {
      this.sessions.set(sessions);
    });
  }
}
