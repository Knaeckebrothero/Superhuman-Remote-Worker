/** Creation clients materialize recommendations; execution admission ignores them. */
export type WorkspaceBinding = {template: {inline: {backend: string}}} | null;
import {WorkspacePreview} from "../../core/models/workspace.model";


export function workspaceCreationFields(
  overrides: Record<string, unknown>, preview?: WorkspacePreview,
): {config_override: Record<string, unknown>; workspace?: Record<string, unknown> | null} {
  const config = {...overrides};
  const privateWorkspace = {...(config['workspace'] as Record<string, unknown> | undefined)};
  const backend = privateWorkspace['backend'];
  // Preserve the existing explicit VM sizing input until the SRW provisioner
  // supports resource-bearing templates. Never discard a caller's sizes.
  if (privateWorkspace['vm']) return {config_override: config};
  delete privateWorkspace['backend'];
  if (Object.keys(privateWorkspace).length) config['workspace'] = privateWorkspace;
  else delete config['workspace'];
  if (typeof backend === 'string') {
    return {config_override: config, workspace: backend === 'none' ? null : {template: {inline: {backend}}}};
  }
  if (preview?.source === 'recommendation') {
    return {config_override: config, workspace: preview.binding};
  }
  return {config_override: config};
}

export function workspacePreviewConfig(config: Record<string, unknown>, preview?: WorkspacePreview): Record<string, unknown> {
  if (!preview) return config;
  return {...config, workspace: {...(config['workspace'] as Record<string, unknown> | undefined), backend: preview.backend}};
}
