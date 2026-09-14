import {Component} from '@angular/core';
import {SkillsListComponent} from './skills-list.component';

@Component({
  selector: 'app-skills-page',
  standalone: true,
  imports: [SkillsListComponent],
  template: `
    <div class="page">
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


      .page-content {
        flex: 1;
        overflow: hidden;
      }
    `,
  ],
})
export class SkillsPageComponent {}
