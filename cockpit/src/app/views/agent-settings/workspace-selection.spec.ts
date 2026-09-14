import {describe, expect, it} from 'vitest';
import {workspaceCreationFields, workspacePreviewConfig} from './workspace-selection';
import {WorkspacePreview} from '../../core/models/workspace.model';

const recommendation: WorkspacePreview = {
  backend: 'sandbox', source: 'recommendation', binding: {template: {inline: {backend: 'sandbox'}}},
};

describe('execution workspace selection', () => {
  it('materializes a recommendation independently of the private configuration', () => {
    const config = {llm: {model: 'chosen'}};
    expect(workspaceCreationFields(config, recommendation)).toEqual({config_override: config, workspace: recommendation.binding});
    expect(config).toEqual({llm: {model: 'chosen'}});
  });

  it('preserves an explicit no-workspace choice over a recommendation', () => {
    const config = {workspace: {backend: 'none', max_read_words: 100}, llm: {model: 'chosen'}};
    expect(workspaceCreationFields(config, recommendation)).toEqual({
      workspace: null, config_override: {workspace: {max_read_words: 100}, llm: {model: 'chosen'}},
    });
    expect(config.workspace.backend).toBe('none');
  });

  it('lets the server select and freeze a Project default', () => {
    expect(workspaceCreationFields({}, {...recommendation, source: 'project'})).toEqual({config_override: {}});
  });

  it('renders selected infrastructure without changing Expert behavior', () => {
    const expert = {workspace: {backend: 'virtual', git_versioning: false}, tools: {shell: ['run_command']}};
    expect(workspacePreviewConfig(expert, recommendation)).toEqual({...expert, workspace: {backend: 'sandbox', git_versioning: false}});
    expect(expert.workspace.backend).toBe('virtual');
  });
});
