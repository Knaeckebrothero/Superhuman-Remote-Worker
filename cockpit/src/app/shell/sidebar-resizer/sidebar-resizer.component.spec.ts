import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {ComponentFixture, TestBed} from '@angular/core/testing';
import {TranslocoTestingModule} from '@jsverse/transloco';
import {SidebarResizerComponent} from './sidebar-resizer.component';
import {
  SIDEBAR_DEFAULT_WIDTH,
  SIDEBAR_MAX_WIDTH,
  SIDEBAR_MIN_WIDTH,
  SIDEBAR_WIDTH_STEP,
  SidebarService,
} from '../../core/services/sidebar.service';

/**
 * Drives the real SidebarService rather than a stub: clamping is the half of
 * this interaction that has teeth, and a stubbed setWidth would let a test pass
 * while the handle happily drags the rail to 4000px.
 */
describe('SidebarResizerComponent', () => {
  const originalMatchMedia = window.matchMedia;
  let fixture: ComponentFixture<SidebarResizerComponent>;
  let sidebar: SidebarService;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    window.localStorage.clear();
    document.body.className = '';
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (window as any).matchMedia = vi.fn().mockReturnValue({matches: false});

    TestBed.configureTestingModule({
      imports: [
        SidebarResizerComponent,
        TranslocoTestingModule.forRoot({
          langs: {en: {nav: {resizeSidebar: 'Resize sidebar'}}},
          translocoConfig: {availableLangs: ['en'], defaultLang: 'en'},
        }),
      ],
    });
    await TestBed.compileComponents();
    fixture = TestBed.createComponent(SidebarResizerComponent);
    sidebar = TestBed.inject(SidebarService);
    fixture.detectChanges();
  });

  afterEach(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (window as any).matchMedia = originalMatchMedia;
    window.localStorage.clear();
    document.body.className = '';
  });

  function handle(): HTMLElement {
    return fixture.nativeElement.querySelector('.handle') as HTMLElement;
  }

  /** jsdom has no PointerEvent — MouseEvent carries clientX, which is all the
   *  component reads, plus a pointerId the capture calls are guarded against. */
  function pointer(type: string, clientX: number, button = 0): Event {
    const event = new MouseEvent(type, {clientX, button, bubbles: true, cancelable: true});
    Object.defineProperty(event, 'pointerId', {value: 1});
    return event;
  }

  function drag(from: number, to: number): void {
    handle().dispatchEvent(pointer('pointerdown', from));
    handle().dispatchEvent(pointer('pointermove', to));
    handle().dispatchEvent(pointer('pointerup', to));
  }

  describe('the ARIA window-splitter contract', () => {
    it('is a focusable separator carrying the width as its value', () => {
      const el = handle();
      expect(el.getAttribute('role')).toBe('separator');
      expect(el.getAttribute('aria-orientation')).toBe('vertical');
      expect(el.getAttribute('tabindex')).toBe('0');
      expect(el.getAttribute('aria-controls')).toBe('sidebar-rail');
      expect(el.getAttribute('aria-valuenow')).toBe(String(SIDEBAR_DEFAULT_WIDTH));
      expect(el.getAttribute('aria-valuemin')).toBe(String(SIDEBAR_MIN_WIDTH));
      expect(el.getAttribute('aria-valuemax')).toBe(String(SIDEBAR_MAX_WIDTH));
    });

    it('keeps aria-valuenow in step with the width', () => {
      drag(260, 340);
      fixture.detectChanges();
      expect(handle().getAttribute('aria-valuenow')).toBe('340');
    });
  });

  describe('drag', () => {
    it('applies the pointer delta to the width the gesture started from', () => {
      drag(260, 350);
      expect(sidebar.width()).toBe(SIDEBAR_DEFAULT_WIDTH + 90);
    });

    // The delta is measured from the start, not read off clientX: the handle
    // straddles the seam, so treating clientX as the width would snap the rail
    // a few px on the first move of every drag.
    it('does not snap the rail to the pointer position on the first move', () => {
      handle().dispatchEvent(pointer('pointerdown', 600));
      handle().dispatchEvent(pointer('pointermove', 600));
      expect(sidebar.width()).toBe(SIDEBAR_DEFAULT_WIDTH);
    });

    it('narrows when dragged left', () => {
      drag(260, 220);
      expect(sidebar.width()).toBe(SIDEBAR_DEFAULT_WIDTH - 40);
    });

    it('clamps at the bounds instead of following the pointer off the range', () => {
      drag(260, 2000);
      expect(sidebar.width()).toBe(SIDEBAR_MAX_WIDTH);
      drag(260, -2000);
      expect(sidebar.width()).toBe(SIDEBAR_MIN_WIDTH);
    });

    it('ignores movement when no gesture is in progress', () => {
      handle().dispatchEvent(pointer('pointermove', 900));
      expect(sidebar.width()).toBe(SIDEBAR_DEFAULT_WIDTH);
    });

    // A right-click drag isn't a resize, and a context-menu-interrupted
    // gesture would strand the body class and the resizing flag.
    it('ignores a non-primary button', () => {
      handle().dispatchEvent(pointer('pointerdown', 260, 2));
      handle().dispatchEvent(pointer('pointermove', 400));
      expect(sidebar.resizing()).toBe(false);
      expect(sidebar.width()).toBe(SIDEBAR_DEFAULT_WIDTH);
    });

    it('persists once, at the end of the gesture', () => {
      const key = 'cockpit:sidebar:width';
      handle().dispatchEvent(pointer('pointerdown', 260));
      handle().dispatchEvent(pointer('pointermove', 300));
      expect(window.localStorage.getItem(key)).toBeNull();
      handle().dispatchEvent(pointer('pointerup', 300));
      expect(window.localStorage.getItem(key)).toBe('300');
    });
  });

  describe('the drag flag and its body class', () => {
    it('is set for the duration of the gesture and cleared after', () => {
      handle().dispatchEvent(pointer('pointerdown', 260));
      expect(sidebar.resizing()).toBe(true);
      expect(document.body.classList.contains('sidebar-resizing')).toBe(true);

      handle().dispatchEvent(pointer('pointerup', 300));
      expect(sidebar.resizing()).toBe(false);
      expect(document.body.classList.contains('sidebar-resizing')).toBe(false);
    });

    it('is cleared by pointercancel, not just pointerup', () => {
      handle().dispatchEvent(pointer('pointerdown', 260));
      handle().dispatchEvent(pointer('pointercancel', 300));
      expect(sidebar.resizing()).toBe(false);
      expect(document.body.classList.contains('sidebar-resizing')).toBe(false);
    });

    // The shell unmounts this component when the rail collapses or the viewport
    // crosses into drawer territory — the body class would otherwise outlive
    // the component that set it and freeze the cursor app-wide.
    it('is cleared when the component is destroyed mid-gesture', () => {
      handle().dispatchEvent(pointer('pointerdown', 260));
      fixture.destroy();
      expect(sidebar.resizing()).toBe(false);
      expect(document.body.classList.contains('sidebar-resizing')).toBe(false);
    });
  });

  describe('double-click', () => {
    it('restores the default width and persists it', () => {
      drag(260, 460);
      handle().dispatchEvent(new MouseEvent('dblclick', {bubbles: true}));
      expect(sidebar.width()).toBe(SIDEBAR_DEFAULT_WIDTH);
      expect(window.localStorage.getItem('cockpit:sidebar:width')).toBe(
        String(SIDEBAR_DEFAULT_WIDTH),
      );
    });
  });

  describe('keyboard', () => {
    function key(name: string): KeyboardEvent {
      const event = new KeyboardEvent('keydown', {key: name, bubbles: true, cancelable: true});
      handle().dispatchEvent(event);
      return event;
    }

    it('nudges by a step per arrow press and persists each nudge', () => {
      key('ArrowRight');
      expect(sidebar.width()).toBe(SIDEBAR_DEFAULT_WIDTH + SIDEBAR_WIDTH_STEP);
      key('ArrowLeft');
      key('ArrowLeft');
      expect(sidebar.width()).toBe(SIDEBAR_DEFAULT_WIDTH - SIDEBAR_WIDTH_STEP);
      expect(window.localStorage.getItem('cockpit:sidebar:width')).toBe(
        String(SIDEBAR_DEFAULT_WIDTH - SIDEBAR_WIDTH_STEP),
      );
    });

    it('jumps to the bounds with Home and End', () => {
      key('End');
      expect(sidebar.width()).toBe(SIDEBAR_MAX_WIDTH);
      key('Home');
      expect(sidebar.width()).toBe(SIDEBAR_MIN_WIDTH);
    });

    it('claims only the keys it handles', () => {
      expect(key('ArrowRight').defaultPrevented).toBe(true);
      // Tab must still move focus, and an unclaimed key must not silently
      // swallow whatever the browser would have done with it.
      expect(key('Tab').defaultPrevented).toBe(false);
      expect(sidebar.width()).toBe(SIDEBAR_DEFAULT_WIDTH + SIDEBAR_WIDTH_STEP);
    });
  });
});
