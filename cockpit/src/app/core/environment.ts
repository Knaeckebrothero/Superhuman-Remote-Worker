const getApiUrl = (): string => {
  if (typeof window !== 'undefined') {
    return (window as any)['env']?.['apiUrl'] || 'http://localhost:8085/api';
  }
  return 'http://localhost:8085/api';
};

const getGiteaUrl = (): string | null => {
  if (typeof window !== 'undefined') {
    return (window as any)['env']?.['giteaUrl'] || null;
  }
  return null;
};

const getDozzleUrl = (): string | null => {
  if (typeof window !== 'undefined') {
    return (window as any)['env']?.['dozzleUrl'] || null;
  }
  return null;
};

const getModels = (): { group: string; models: string[] }[] => {
  if (typeof window !== 'undefined') {
    return (window as any)['env']?.['models'] || [];
  }
  return [];
};

const getModelPresets = (): { label: string; strategic: string; tactical: string }[] => {
  if (typeof window !== 'undefined') {
    return (window as any)['env']?.['modelPresets'] || [];
  }
  return [];
};

const getBuilderModels = (): { label: string; id: string }[] => {
  if (typeof window !== 'undefined') {
    const models = (window as any)['env']?.['builderModels'];
    if (Array.isArray(models) && models.length > 0) return models;
  }
  return [
    { label: 'GPT-5.2 Pro', id: 'gpt-5.2-pro' },
    { label: 'Claude Opus 4.6', id: 'claude-opus-4-6' },
  ];
};

export const environment = {
  apiUrl: getApiUrl(),
  giteaUrl: getGiteaUrl(),
  dozzleUrl: getDozzleUrl(),
  models: getModels(),
  modelPresets: getModelPresets(),
  builderModels: getBuilderModels(),
};
