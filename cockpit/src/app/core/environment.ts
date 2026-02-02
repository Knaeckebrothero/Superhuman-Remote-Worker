const getApiUrl = (): string => {
  if (typeof window !== 'undefined') {
    return (window as any)['env']?.['apiUrl'] || 'http://localhost:8085/api';
  }
  return 'http://localhost:8085/api';
};

export const environment = {
  apiUrl: getApiUrl(),
};
