import { Component } from '@angular/core';
import { MobileShellComponent } from '../../layout/mobile-shell/mobile-shell.component';

@Component({
  selector: 'app-shell-page',
  standalone: true,
  imports: [MobileShellComponent],
  template: `<app-mobile-shell />`,
  styles: [
    `
      :host {
        display: block;
        height: 100%;
      }
    `,
  ],
})
export class ShellPageComponent {}
