import { Component, signal, HostListener, ElementRef, inject } from '@angular/core';
import { LayoutService } from '../../core/services/layout.service';
import { LayoutPickerComponent } from '../layout-picker/layout-picker.component';
import { environment } from '../../core/environment';

interface MenuLink {
  label: string;
  url: string;
  icon: string;
  description: string;
}

interface MenuSection {
  title: string;
  items: MenuLink[];
}

@Component({
  selector: 'app-menu',
  imports: [LayoutPickerComponent],
  template: `
    <div class="menu-container">
      <button
        class="menu-button"
        (click)="toggleMenu()"
        [class.active]="isOpen()"
        aria-label="Open menu"
      >
        <svg viewBox="0 0 24 24" fill="currentColor">
          @if (isOpen()) {
            <path d="M6 18L18 6M6 6l12 12" stroke="currentColor" stroke-width="2" fill="none" />
          } @else {
            <rect x="3" y="5" width="18" height="2" rx="1" />
            <rect x="3" y="11" width="18" height="2" rx="1" />
            <rect x="3" y="17" width="18" height="2" rx="1" />
          }
        </svg>
      </button>

      @if (isOpen()) {
        <div class="menu-popup">
          @for (section of menuSections; track section.title) {
            <div class="menu-section">
              <h3 class="section-title">{{ section.title }}</h3>
              @for (item of section.items; track item.label) {
                <a
                  class="menu-item"
                  [href]="item.url"
                  target="_blank"
                  rel="noopener noreferrer"
                  (click)="closeMenu()"
                >
                  <span class="item-icon">{{ item.icon }}</span>
                  <div class="item-content">
                    <span class="item-label">{{ item.label }}</span>
                    <span class="item-description">{{ item.description }}</span>
                  </div>
                  <svg class="external-icon" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
                  </svg>
                </a>
              }
            </div>
          }

          <!-- Layout Picker Trigger -->
          <div class="menu-section layout-section">
            <h3 class="section-title">Layouts</h3>
            <button
              #layoutBtn
              class="menu-item"
              (click)="toggleLayoutPicker($event, layoutBtn)"
              [class.active]="isLayoutPickerOpen()"
            >
              <span class="item-icon">📐</span>
              <div class="item-content">
                <span class="item-label">Choose Layout</span>
                <span class="item-description">Select panel arrangement</span>
              </div>
              <svg class="chevron-icon" viewBox="0 0 24 24" fill="currentColor">
                <path d="M9 6l6 6-6 6" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
            </button>
            @if (isLayoutPickerOpen()) {
              <app-layout-picker
                [top]="pickerTop()"
                [left]="pickerLeft()"
                (closed)="closeLayoutPicker()"
              />
            }
          </div>

          <div class="menu-section">
            <h3 class="section-title">Settings</h3>
            <div class="menu-item settings-item">
              <span class="item-icon">🎨</span>
              <div class="item-content">
                <span class="item-label">Theme</span>
                <span class="item-description">Dark (Catppuccin Mocha)</span>
              </div>
            </div>
            <button class="menu-item" (click)="resetLayout()">
              <span class="item-icon">🔄</span>
              <div class="item-content">
                <span class="item-label">Reset Layout</span>
                <span class="item-description">Restore default panel arrangement</span>
              </div>
            </button>
          </div>

          <div class="menu-footer">
            <span>Debug Cockpit v0.1.0</span>
          </div>
        </div>
      }
    </div>
  `,
  styles: [
    `
      .menu-container {
        position: relative;
      }

      .menu-button {
        display: flex;
        align-items: center;
        justify-content: center;
        width: 36px;
        height: 36px;
        border: none;
        border-radius: 6px;
        background: transparent;
        color: var(--text-secondary, #a6adc8);
        cursor: pointer;
        transition: background 0.15s, color 0.15s;
      }

      .menu-button:hover,
      .menu-button.active {
        background: var(--surface-0, #313244);
        color: var(--text-primary, #cdd6f4);
      }

      .menu-button svg {
        width: 20px;
        height: 20px;
      }

      .menu-popup {
        position: absolute;
        top: calc(100% + 8px);
        left: 0;
        width: 280px;
        max-height: 80vh;
        overflow-y: auto;
        background: var(--panel-bg, #181825);
        border: 1px solid var(--border-color, #313244);
        border-radius: 8px;
        box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
        z-index: 1000;
      }

      .menu-section {
        padding: 8px;
        border-bottom: 1px solid var(--border-color, #313244);
      }

      .menu-section:last-of-type {
        border-bottom: none;
      }

      .section-title {
        font-size: 10px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 1px;
        color: var(--text-muted, #6c7086);
        padding: 4px 8px;
        margin: 0;
      }

      .menu-item {
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 10px 12px;
        border-radius: 6px;
        text-decoration: none;
        color: var(--text-primary, #cdd6f4);
        cursor: pointer;
        transition: background 0.15s;
        border: none;
        background: transparent;
        width: 100%;
        text-align: left;
      }

      .menu-item:hover {
        background: var(--surface-0, #313244);
      }

      .settings-item {
        cursor: default;
      }

      .settings-item:hover {
        background: transparent;
      }

      .item-icon {
        font-size: 18px;
        width: 24px;
        text-align: center;
        flex-shrink: 0;
      }

      .item-content {
        flex: 1;
        min-width: 0;
      }

      .item-label {
        display: block;
        font-size: 13px;
        font-weight: 500;
      }

      .item-description {
        display: block;
        font-size: 11px;
        color: var(--text-muted, #6c7086);
        margin-top: 2px;
      }

      .external-icon {
        width: 14px;
        height: 14px;
        color: var(--text-muted, #6c7086);
        flex-shrink: 0;
      }

      .chevron-icon {
        width: 14px;
        height: 14px;
        color: var(--text-muted, #6c7086);
        flex-shrink: 0;
      }

      .menu-item.active .chevron-icon {
        color: var(--accent, #89b4fa);
      }

      .layout-section {
        position: relative;
      }

      .menu-footer {
        padding: 8px 12px;
        background: var(--timeline-bg, #11111b);
        font-size: 10px;
        color: var(--text-muted, #6c7086);
        text-align: center;
      }
    `,
  ],
})
export class MenuComponent {
  private readonly elementRef = inject(ElementRef);
  readonly layoutService = inject(LayoutService);

  readonly isOpen = signal(false);
  readonly isLayoutPickerOpen = signal(false);
  readonly pickerTop = signal(0);
  readonly pickerLeft = signal(0);

  readonly menuSections: MenuSection[] = [
    {
      title: 'Databases',
      items: [
        {
          label: 'Neo4j Browser',
          url: 'http://localhost:7474',
          icon: '🔵',
          description: 'Knowledge graph explorer',
        },
        {
          label: 'PostgreSQL',
          url: 'http://localhost:5050',
          icon: '🐘',
          description: 'pgAdmin database manager',
        },
        {
          label: 'MongoDB',
          url: 'http://localhost:8081',
          icon: '🍃',
          description: 'Mongo Express viewer',
        },
      ],
    },
    {
      title: 'APIs',
      items: [
        {
          label: 'Creator Agent',
          url: 'http://localhost:8001/docs',
          icon: '📝',
          description: 'OpenAPI documentation',
        },
        {
          label: 'Validator Agent',
          url: 'http://localhost:8002/docs',
          icon: '✅',
          description: 'OpenAPI documentation',
        },
      ],
    },
    ...(environment.giteaUrl || environment.dozzleUrl
      ? [
          {
            title: 'Tools',
            items: [
              ...(environment.giteaUrl
                ? [
                    {
                      label: 'Gitea',
                      url: environment.giteaUrl,
                      icon: '🍵',
                      description: 'Git repository browser',
                    },
                  ]
                : []),
              ...(environment.dozzleUrl
                ? [
                    {
                      label: 'Dozzle',
                      url: environment.dozzleUrl,
                      icon: '📋',
                      description: 'Container log viewer',
                    },
                  ]
                : []),
            ],
          },
        ]
      : []),
  ];

  @HostListener('document:click', ['$event'])
  onDocumentClick(event: MouseEvent): void {
    if (!this.elementRef.nativeElement.contains(event.target)) {
      this.isOpen.set(false);
    }
  }

  @HostListener('document:keydown.escape')
  onEscape(): void {
    this.isOpen.set(false);
  }

  toggleMenu(): void {
    this.isOpen.update((v) => !v);
  }

  closeMenu(): void {
    this.isOpen.set(false);
    this.isLayoutPickerOpen.set(false);
  }

  toggleLayoutPicker(event: MouseEvent, buttonEl: HTMLButtonElement): void {
    event.stopPropagation();
    if (!this.isLayoutPickerOpen()) {
      // Calculate position based on button location
      const rect = buttonEl.getBoundingClientRect();
      this.pickerTop.set(rect.top);
      this.pickerLeft.set(rect.right + 8); // 8px gap
    }
    this.isLayoutPickerOpen.update((v) => !v);
  }

  closeLayoutPicker(): void {
    this.isLayoutPickerOpen.set(false);
    this.closeMenu();
  }

  resetLayout(): void {
    this.layoutService.resetLayout();
    this.closeMenu();
  }
}
