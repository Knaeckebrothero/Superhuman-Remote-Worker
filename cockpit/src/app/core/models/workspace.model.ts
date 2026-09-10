export interface WorkspacePreview {
  backend: string;
  source: 'request' | 'project' | 'default' | 'recommendation';
  binding: Record<string, unknown> | null;
}
