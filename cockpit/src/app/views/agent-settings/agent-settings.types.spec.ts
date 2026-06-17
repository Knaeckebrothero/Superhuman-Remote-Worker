import {describe, it, expect} from 'vitest';
import {detectModelFamily} from './agent-settings.types';

describe('detectModelFamily — GLM', () => {
  it('maps GLM-5.2 IDs to the glm family across transports', () => {
    expect(detectModelFamily('openrouter/z-ai/glm-5.2')).toBe('glm');
    expect(detectModelFamily('z-ai/glm-5.2')).toBe('glm');
    expect(detectModelFamily('glm-5.2')).toBe('glm');
  });
});

describe('detectModelFamily — Mistral', () => {
  it('maps Mistral 3 family + specialists across transports', () => {
    expect(detectModelFamily('mistral-large-latest')).toBe('mistral');
    expect(detectModelFamily('mistral-medium-latest')).toBe('mistral');
    expect(detectModelFamily('mistral-small-latest')).toBe('mistral');
    expect(detectModelFamily('codestral-latest')).toBe('mistral');
    expect(detectModelFamily('openrouter/mistralai/mistral-large')).toBe('mistral');
  });
});
