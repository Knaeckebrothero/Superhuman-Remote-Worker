import {describe, expect, it} from 'vitest';
import {formatTokens, parseTokens} from './format-tokens';

describe('formatTokens', () => {
  it('renders exact decimal thousands/millions', () => {
    expect(formatTokens(128000)).toBe('128k');
    expect(formatTokens(512000)).toBe('512k');
    expect(formatTokens(32000)).toBe('32k');
    expect(formatTokens(200000)).toBe('200k');
    expect(formatTokens(1_000_000)).toBe('1M');
    expect(formatTokens(1_050_000)).toBe('1.05M');
  });

  it('renders exact binary (×1024) values with clean labels', () => {
    expect(formatTokens(65_536)).toBe('64k');
    expect(formatTokens(131_072)).toBe('128k');
    expect(formatTokens(262_144)).toBe('256k');
    expect(formatTokens(393_216)).toBe('384k');
    expect(formatTokens(524_288)).toBe('512k');
    expect(formatTokens(1_048_576)).toBe('1M');
  });

  it('falls back to decimal-rounded for irregular values', () => {
    expect(formatTokens(123456)).toBe('123k');
  });

  it('renders sub-1000 values as-is', () => {
    expect(formatTokens(512)).toBe('512');
    expect(formatTokens(0)).toBe('0');
  });
});

describe('parseTokens', () => {
  it('parses ×1024 k/M suffixes (case-insensitive)', () => {
    expect(parseTokens('128k')).toBe(131_072);
    expect(parseTokens('64k')).toBe(65_536);
    expect(parseTokens('512K')).toBe(524_288);
    expect(parseTokens('1M')).toBe(1_048_576);
    expect(parseTokens('1.5m')).toBe(1_572_864);
  });

  it('parses raw numbers typed directly', () => {
    expect(parseTokens('131072')).toBe(131_072);
    expect(parseTokens('300000')).toBe(300_000);
    expect(parseTokens(' 256000 ')).toBe(256_000);
  });

  it('returns null for empty/invalid input', () => {
    expect(parseTokens('')).toBeNull();
    expect(parseTokens('   ')).toBeNull();
    expect(parseTokens('abc')).toBeNull();
    expect(parseTokens('12kb')).toBeNull();
  });

  it('round-trips the presets through formatTokens', () => {
    for (const label of ['64k', '128k', '256k', '384k', '512k', '1M']) {
      expect(formatTokens(parseTokens(label)!)).toBe(label);
    }
  });
});
