import {describe, expect, it} from 'vitest';
import de from '../../../assets/i18n/de-DE.json';
import en from '../../../assets/i18n/en.json';

describe('connector product terminology', () => {
  it('uses Connectors for the English navigation and workflow', () => {
    expect(en.nav.datasources).toBe('Connectors');
    expect(en.projectDetail.tabs.datasources).toBe('Connectors');
    expect(en.datasources.title).toBe('Connectors');
    expect(en.datasources.form.newTitle).toBe('New Connector');
    expect(en.datasources.form.typeLabel).toBe('Connector Type');
    expect(en.agentSettings.datasources.group).toBe('Connectors');
  });

  it('uses the equivalent connector term in German', () => {
    expect(de.nav.datasources).toBe('Konnektoren');
    expect(de.projectDetail.tabs.datasources).toBe('Konnektoren');
    expect(de.datasources.title).toBe('Konnektoren');
    expect(de.datasources.form.newTitle).toBe('Neuer Konnektor');
    expect(de.datasources.form.typeLabel).toBe('Konnektortyp');
    expect(de.agentSettings.datasources.group).toBe('Konnektoren');
  });
});
