import {describe, it, expect} from 'vitest';
import {detectModelFamily} from './agent-settings.types';

describe('detectModelFamily — GLM', () => {
  it('maps GLM-5.2 IDs to the glm family across transports', () => {
    expect(detectModelFamily('openrouter/z-ai/glm-5.2')).toBe('glm');
    expect(detectModelFamily('z-ai/glm-5.2')).toBe('glm');
    expect(detectModelFamily('glm-5.2')).toBe('glm');
  });
});
