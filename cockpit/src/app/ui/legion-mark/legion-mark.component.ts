import {ChangeDetectionStrategy, Component, Input} from '@angular/core';

/**
 * SRW Legion Mark — modernized Roman standard / Aquila silhouette.
 * Inherits color from currentColor for the staff/banner/base, and uses
 * --accent-color for the finial dot and chevrons.
 */
@Component({
  selector: 'srw-legion-mark',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <svg
      [attr.width]="size"
      [attr.height]="size"
      viewBox="0 0 32 32"
      fill="none"
      [attr.aria-label]="ariaLabel"
      [attr.role]="ariaLabel ? 'img' : 'presentation'"
    >
      <line x1="16" y1="4" x2="16" y2="28"
            stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
      <circle cx="16" cy="4.5" r="1.5" fill="var(--accent-color)" />
      <path d="M8 9 L16 11 L24 9"
            stroke="var(--accent-color)" stroke-width="1.5"
            stroke-linecap="round" stroke-linejoin="round" fill="none" />
      <path d="M9 12 L16 14 L23 12"
            stroke="var(--accent-color)" stroke-width="1.2"
            stroke-linecap="round" stroke-linejoin="round" fill="none"
            opacity="0.7" />
      <rect x="10" y="17" width="12" height="6"
            stroke="currentColor" stroke-width="1.2" fill="none" />
      <line x1="10" y1="20" x2="22" y2="20"
            stroke="currentColor" stroke-width="1" opacity="0.5" />
      <path d="M13 26 L19 26"
            stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
    </svg>
  `,
  styles: [`
    :host {
      display: inline-flex;
      color: var(--text-primary);
      line-height: 0;
    }
  `],
})
export class LegionMarkComponent {
  @Input() size: number = 24;
  @Input() ariaLabel?: string;
}
