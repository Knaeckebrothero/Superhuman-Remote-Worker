import {Component, signal, Type} from '@angular/core';
import {NgComponentOutlet} from '@angular/common';

/**
 * Keeps the large connector editor out of the initial application bundle while
 * preserving its use as a dynamically registered layout panel.
 */
@Component({
  selector: 'app-datasource-list-loader',
  imports: [NgComponentOutlet],
  template: `
    @if (component(); as loaded) {
      <ng-container *ngComponentOutlet="loaded" />
    }
  `,
  styles: `:host { display: block; height: 100%; }`,
})
export class DatasourceListLoaderComponent {
  readonly component = signal<Type<unknown> | null>(null);

  constructor() {
    void import('./datasource-list.component').then(({DatasourceListComponent}) => {
      this.component.set(DatasourceListComponent);
    });
  }
}
