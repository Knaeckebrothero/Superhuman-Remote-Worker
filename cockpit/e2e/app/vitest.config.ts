import { defineConfig } from 'vitest/config';

export default defineConfig({
  root: 'e2e/app',
  test: {
    environment: 'node',
    include: ['*.test.ts'],
  },
});
