import { Component } from '@angular/core';

@Component({
  selector: 'app-placeholder-b',
  template: `
    <div class="placeholder">
      <span class="placeholder-label">B</span>
      <span class="placeholder-text">Agent Chat</span>
    </div>
  `,
  styles: [
    `
      .placeholder {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        height: 100%;
        gap: 8px;
        color: var(--text-muted, #6c7086);
      }

      .placeholder-label {
        font-size: 48px;
        font-weight: 700;
        opacity: 0.3;
      }

      .placeholder-text {
        font-size: 14px;
        text-transform: uppercase;
        letter-spacing: 2px;
      }
    `,
  ],
})
export class PlaceholderBComponent {}
