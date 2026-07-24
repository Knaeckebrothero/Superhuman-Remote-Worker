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
  // Isolated Dynamic Canvas viewer suffix (for example
  // `.canvas.example-userland.com`). Null keeps live apps dark-shipped.
  canvasViewerHostSuffix: getEnvOrNull('canvasViewerHostSuffix'),
  // Exact public Collabora origin. Null keeps Office Canvas viewing disabled.
  canvasOfficeOrigin: getEnvOrNull('canvasOfficeOrigin'),

  // External tools
  giteaUrl: getEnv('giteaUrl', 'http://localhost:3000/srw'),
  dozzleUrl: getEnv('dozzleUrl', 'http://localhost:9999'),
  minioConsoleUrl: getEnvOrNull('minioConsoleUrl'),
  cloudUrl: getEnvOrNull('cloudUrl'),
  neo4jUrl: getEnv('neo4jUrl', 'http://localhost:7474'),
  pgadminUrl: getEnv('pgadminUrl', 'http://localhost:5050'),
  mcpUrl: getEnv('mcpUrl', 'http://localhost:8055/mcp'),
};
