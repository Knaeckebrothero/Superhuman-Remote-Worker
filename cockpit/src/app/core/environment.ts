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

export const environment = {
  // Core
  apiUrl: getEnv('apiUrl', 'http://localhost:8085/api'),

  // External tools
  giteaUrl: getEnv('giteaUrl', 'http://localhost:3000/srw'),
  dozzleUrl: getEnv('dozzleUrl', 'http://localhost:9999'),
  minioConsoleUrl: getEnvOrNull('minioConsoleUrl'),
  cloudUrl: getEnvOrNull('cloudUrl'),
  neo4jUrl: getEnv('neo4jUrl', 'http://localhost:7474'),
  pgadminUrl: getEnv('pgadminUrl', 'http://localhost:5050'),
  mongoExpressUrl: getEnv('mongoExpressUrl', 'http://localhost:8081'),
  mcpUrl: getEnv('mcpUrl', 'http://localhost:8055/mcp'),

  // Keycloak SSO
  keycloakUrl: getEnv('keycloakUrl', 'http://localhost:8180'),
  keycloakRealm: getEnv('keycloakRealm', 'srw'),
  keycloakClientId: getEnv('keycloakClientId', 'cockpit'),
};
