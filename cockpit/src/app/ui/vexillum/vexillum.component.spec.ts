import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {dirname, join} from 'node:path';
import {beforeEach, describe, expect, it} from 'vitest';
import {TestBed} from '@angular/core/testing';
import {VexillumComponent} from './vexillum.component';

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, 'vexillum.component.ts'), 'utf8');

describe('VexillumComponent', () => {
  beforeEach(async () => {
    await TestBed.configureTestingModule({imports: [VexillumComponent]}).compileComponents();
  });

  it('renders the standard with the SRW lettering, hidden from assistive tech', () => {
    const fixture = TestBed.createComponent(VexillumComponent);
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('text.lettering')?.textContent).toBe('SRW');
    expect(el.querySelector('rect.banner')).not.toBeNull();
    const svg = el.querySelector('svg');
    expect(svg?.getAttribute('role')).toBe('presentation');
    expect(svg?.getAttribute('aria-hidden')).toBe('true');
  });

  // The whole point of drawing it inline: the banner follows the accent
  // axis, so no literal red may creep back in.
  it('paints banner and lettering from the accent tokens, never a literal', () => {
    expect(source).toMatch(/\.banner\s*\{\s*fill:\s*var\(--accent-color\)/);
    expect(source).toMatch(/\.lettering\s*\{[^}]*fill:\s*var\(--on-accent\)/);
    expect(source).not.toMatch(/#(9c1f2e|9c2832|cc4647|7a3b1a|fbf6ec)/i);
  });
});
