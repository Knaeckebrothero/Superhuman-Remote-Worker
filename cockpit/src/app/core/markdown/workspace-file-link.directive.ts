import {
  AfterViewInit,
  Directive,
  ElementRef,
  EventEmitter,
  OnDestroy,
  Output,
  inject,
} from '@angular/core';
import {WORKSPACE_FILE_LINK_CLASS, WORKSPACE_FILE_PATH_ATTR} from './link-extension';

/**
 * Makes the inert workspace-file controls emitted by `markdownLinkExtension`
 * do something, on the surfaces that know what a workspace file is.
 *
 * The parser emits `<button class="workspace-file-link" data-workspace-file-path>`
 * for every agent-written relative link. It carries no href by construction, so
 * a host that forgets this directive renders a dead control rather than a link
 * that would navigate the Cockpit document away from the running session.
 *
 * Click handling is delegated from the `<markdown>` host, so it survives every
 * re-render of the message body without re-binding.
 */
@Directive({
  selector: 'markdown[appWorkspaceFileLink]',
  standalone: true,
})
export class WorkspaceFileLinkDirective implements AfterViewInit, OnDestroy {
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);

  /**
   * The normalized workspace path of the control the reader activated.
   *
   * Declared with `@Output()` rather than `output()`: an initializer-based
   * output on a *directive* is not reflected into host template bindings under
   * the spec compiler, so the wiring this fix depends on could not be asserted
   * in a test (`PinOnInteractDirective` has the same blind spot).
   */
  @Output() readonly workspaceFileOpen = new EventEmitter<string>();

  ngAfterViewInit(): void {
    this.host.nativeElement.addEventListener('click', this.onClick);
  }

  ngOnDestroy(): void {
    this.host.nativeElement.removeEventListener('click', this.onClick);
  }

  private readonly onClick = (event: Event): void => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const control = target.closest<HTMLElement>(
      `button.${WORKSPACE_FILE_LINK_CLASS}[${WORKSPACE_FILE_PATH_ATTR}]`,
    );
    const path = control?.getAttribute(WORKSPACE_FILE_PATH_ATTR);
    if (!path) return;
    event.preventDefault();
    this.workspaceFileOpen.emit(path);
  };
}
