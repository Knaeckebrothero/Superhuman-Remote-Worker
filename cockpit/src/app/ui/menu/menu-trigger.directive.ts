import {
  Directive,
  ElementRef,
  HostListener,
  inject,
  input,
} from '@angular/core';
import {AppMenuComponent, MenuPlacement} from './menu.component';

@Directive({
  selector: '[appMenuTrigger]',
  standalone: true,
  host: {
    'aria-haspopup': 'menu',
    '[attr.aria-expanded]': 'isOpen()',
  },
})
export class AppMenuTriggerDirective {
  appMenuTrigger = input.required<AppMenuComponent>();
  menuPlacement = input<MenuPlacement>('bottom-start');

  private host = inject(ElementRef<HTMLElement>);

  protected isOpen(): boolean {
    return this.appMenuTrigger().isOpen();
  }

  @HostListener('click', ['$event'])
  protected onClick(event: MouseEvent): void {
    event.stopPropagation();
    this.appMenuTrigger().toggle(this.host.nativeElement, this.menuPlacement());
  }

  @HostListener('keydown.arrowdown', ['$event'])
  protected onArrowDown(event: Event): void {
    event.preventDefault();
    this.appMenuTrigger().open(this.host.nativeElement, this.menuPlacement());
  }
}
