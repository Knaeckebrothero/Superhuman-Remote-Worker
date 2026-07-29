import {Directive, HostListener, output} from '@angular/core';

/**
 * Emit when the user actually interacts with a control that is displaying an
 * inherited value, so the host can commit that value as an explicit override.
 *
 * Every settings control renders `override() ?? resolved()`, which means the
 * value on screen may be a value the user chose OR one merely inherited from
 * the expert/account/framework layers — the two are indistinguishable to them.
 * A native `<select>` fires no `change` event when you re-pick the option
 * already showing, so the displayed value was the one option the form could
 * not express: you could see "Container", click "Container", and submit a body
 * that said nothing about the backend.
 *
 * Deliberate interaction is the intent signal the browser refuses to give us.
 * `mousedown` covers opening the dropdown; `keydown` covers keyboard use
 * (Tab-to-focus fires its keydown on the element being left, so merely tabbing
 * past a control does not pin it). The host decides what to commit — see the
 * `pin()` helpers, which are no-ops once an override exists, so this stays
 * idempotent and never overwrites a real choice.
 */
@Directive({
  selector: '[appPinOnInteract]',
  standalone: true,
})
export class PinOnInteractDirective {
  readonly pin = output<void>();

  @HostListener('mousedown')
  @HostListener('keydown')
  protected onInteract(): void {
    this.pin.emit();
  }
}
