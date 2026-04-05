const getEnv = (key: string, fallback: string): string => {
  if (typeof window !== 'undefined') {
    return (window as any)['env']?.[key] || fallback;
  }
  return fallback;
};

const getEnvOrNull = (key: string): string | null => {
  if (typeof window !== 'undefined') {
    return (window as any)['env']?.[key] || null;
  }
  return null;
};

const getEnvArray = <T>(key: string, fallback: T[] = []): T[] => {
  if (typeof window !== 'undefined') {
    const val = (window as any)['env']?.[key];
    if (Array.isArray(val) && val.length > 0) return val;
  }
  return fallback;
};

export const environment = {
  // Core
  apiUrl: getEnv('apiUrl', 'http://localhost:8085/api'),

  // External tools
  giteaUrl: getEnv('giteaUrl', 'http://localhost:3000/srw'),
  dozzleUrl: getEnv('dozzleUrl', 'http://localhost:9999'),
  minioConsoleUrl: getEnvOrNull('minioConsoleUrl'),
  nextcloudUrl: getEnvOrNull('nextcloudUrl'),
  neo4jUrl: getEnv('neo4jUrl', 'http://localhost:7474'),
  pgadminUrl: getEnv('pgadminUrl', 'http://localhost:5050'),
  mongoExpressUrl: getEnv('mongoExpressUrl', 'http://localhost:8081'),

  // Keycloak SSO
  keycloakUrl: getEnv('keycloakUrl', 'http://localhost:8180'),
  keycloakRealm: getEnv('keycloakRealm', 'srw'),
  keycloakClientId: getEnv('keycloakClientId', 'cockpit'),

  // Model configuration
  models: getEnvArray<{ group: string; models: string[] }>('models'),
  modelPresets: getEnvArray<{ label: string; strategic: string; tactical: string }>('modelPresets'),
  builderModels: getEnvArray<{ label: string; id: string }>('builderModels', [
    { label: 'GPT OSS 120B (Local)', id: 'openai/gpt-oss-120b' },
    { label: 'MiniMax M2.7', id: 'openrouter/minimax/minimax-m2.7' },
    { label: 'GPT-5.4', id: 'gpt-5.4' },
    { label: 'Codex (coding)', id: 'codex/gpt-5.3-codex' },
    { label: 'Codex Spark (ultra-fast)', id: 'codex/gpt-5.3-codex-spark' },
    { label: 'Claude Opus 4.6', id: 'claude-opus-4-6' },
  ]),
};
