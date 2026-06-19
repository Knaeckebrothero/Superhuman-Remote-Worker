import { defineConfig, configDefaults } from 'vitest/config';

export default defineConfig({
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test-setup.ts'],
    include: ['src/**/*.spec.ts'],
    // _parked/ holds reference snapshots of the removed builder (canvas seed) —
    // never compiled or tested. See core/services/_parked/README.md.
    exclude: [...configDefaults.exclude, '**/_parked/**'],
    coverage: {
      reporter: ['text', 'json', 'html'],
      exclude: ['node_modules/', 'src/test-setup.ts'],
    },
  },
});
