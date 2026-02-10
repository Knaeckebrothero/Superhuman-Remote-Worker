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

export const environment = {
  apiUrl: getApiUrl(),
  giteaUrl: getGiteaUrl(),
  dozzleUrl: getDozzleUrl(),
  models: getModels(),
};
