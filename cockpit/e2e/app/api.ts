import type { APIResponse } from '@playwright/test';

/**
 * Fail without printing a response body. E2E responses may contain prompts,
 * cookies, or deployment details; status + the caller-owned label is enough
 * to find the matching sanitized network/cluster diagnostics.
 */
export async function requireJson<T>(response: APIResponse, label: string): Promise<T> {
  if (!response.ok()) {
    throw new Error(`${label} failed with HTTP ${response.status()}.`);
  }
  try {
    return (await response.json()) as T;
  } catch {
    throw new Error(`${label} returned non-JSON content with HTTP ${response.status()}.`);
  }
}

export function requireOk(response: APIResponse, label: string): void {
  if (!response.ok()) throw new Error(`${label} failed with HTTP ${response.status()}.`);
}

export function normalizedUrl(raw: string): string {
  const parsed = new URL(raw);
  parsed.pathname = parsed.pathname.replace(/\/$/, '');
  parsed.search = '';
  parsed.hash = '';
  return parsed.toString().replace(/\/$/, '');
}
