import {Component} from '@angular/core';
import {SidebarToggleComponent} from '../../shell/sidebar-toggle/sidebar-toggle.component';
import {SkillsListComponent} from './skills-list.component';

@Component({
  selector: 'app-skills-page',
  standalone: true,
  imports: [SidebarToggleComponent, SkillsListComponent],
  template: `
    <div class="page">
      <div class="page-toggle">
        <app-sidebar-toggle />
      </div>
      <main class="page-content">
        <app-skills-list />
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

      .page-toggle {
        padding: 8px 12px;
        flex-shrink: 0;
      }

      .page-toggle:empty {
        display: none;
      }

      .page-content {
        flex: 1;
        overflow: hidden;
      }
    `,
  ],
})
export class SkillsPageComponent {}
