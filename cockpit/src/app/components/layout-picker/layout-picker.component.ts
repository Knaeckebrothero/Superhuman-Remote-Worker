import {
  Component,
  inject,
  output,
  input,
  ElementRef,
  HostListener,
  OnInit,
  OnDestroy,
} from '@angular/core';
import { LayoutService } from '../../core/services/layout.service';
import { LayoutPreviewComponent } from './layout-preview.component';

/**
 * Popup component for selecting layout presets with visual SVG previews.
 * Shows featured layouts prominently at top, with other layouts below.
 */
@Component({
  selector: 'app-layout-picker',
  imports: [LayoutPreviewComponent],
  template: `
    <div
      class="layout-picker"
      [style.top.px]="top()"
      [style.left.px]="left()"
      (click)="$event.stopPropagation()"
    >
      <!-- Featured Section -->
      @if (layoutService.featuredPresets().length > 0) {
        <div class="picker-section">
          <h4 class="section-title">Featured</h4>
          <div class="layout-grid">
            @for (preset of layoutService.featuredPresets(); track preset.id) {
              <button
                class="layout-card"
                (click)="selectLayout(preset.id)"
                [title]="preset.description || preset.name"
              >
                <app-layout-preview [config]="preset.config" [width]="72" [height]="45" />
                <span class="card-name">{{ preset.name }}</span>
              </button>
            }
          </div>
        </div>
      }

      <!-- Other Layouts Section -->
      @if (layoutService.otherPresets().length > 0) {
        <div class="picker-section">
          <h4 class="section-title">More Layouts</h4>
          <div class="layout-grid">
            @for (preset of layoutService.otherPresets(); track preset.id) {
              <button
                class="layout-card"
                (click)="selectLayout(preset.id)"
                [title]="preset.description || preset.name"
              >
                <app-layout-preview [config]="preset.config" [width]="72" [height]="45" />
                <span class="card-name">{{ preset.name }}</span>
              </button>
            }
          </div>
        </div>
      }

      <!-- Loading state -->
      @if (layoutService.availablePresets().length === 0) {
        <div class="picker-section">
          <div class="loading">Loading presets...</div>
        </div>
      }
    </div>
  `,
  styles: [
    `
      .layout-picker {
        position: fixed;
        width: 320px;
        max-height: 400px;
        overflow-y: auto;
        background: var(--panel-bg, #181825);
        border: 1px solid var(--border-color, #313244);
        border-radius: 8px;
        box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
        z-index: 1001;
      }

      .picker-section {
        padding: 12px;
        border-bottom: 1px solid var(--border-color, #313244);
      }

      .picker-section:last-child {
        border-bottom: none;
      }

      .section-title {
        font-size: 10px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 1px;
        color: var(--text-muted, #6c7086);
        margin: 0 0 10px 0;
      }

      .layout-grid {
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 8px;
      }

      .layout-card {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 6px;
        padding: 8px;
        background: var(--surface-0, #313244);
        border: 1px solid transparent;
        border-radius: 6px;
        cursor: pointer;
        transition: border-color 0.15s, background 0.15s;
      }

      .layout-card:hover {
        background: var(--surface-1, #45475a);
        border-color: var(--accent, #89b4fa);
      }

      .card-name {
        font-size: 10px;
        color: var(--text-secondary, #a6adc8);
        text-align: center;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        max-width: 100%;
      }

      .loading {
        text-align: center;
        color: var(--text-muted, #6c7086);
        font-size: 12px;
        padding: 20px;
      }
    `,
  ],
})
export class LayoutPickerComponent implements OnInit, OnDestroy {
  private readonly elementRef = inject(ElementRef);
  readonly layoutService = inject(LayoutService);

  /** Fixed position from top of viewport */
  readonly top = input(0);
  /** Fixed position from left of viewport */
  readonly left = input(0);

  /** Emitted when a layout is selected or popup should close */
  readonly closed = output<void>();

  private escapeHandler = (event: KeyboardEvent) => {
    if (event.key === 'Escape') {
      this.closed.emit();
    }
  };

  ngOnInit(): void {
    document.addEventListener('keydown', this.escapeHandler);
  }

  ngOnDestroy(): void {
    document.removeEventListener('keydown', this.escapeHandler);
  }

  @HostListener('document:click', ['$event'])
  onDocumentClick(event: MouseEvent): void {
    if (!this.elementRef.nativeElement.contains(event.target)) {
      this.closed.emit();
    }
  }

  selectLayout(presetId: string): void {
    this.layoutService.applyPreset(presetId);
    this.closed.emit();
  }
}
