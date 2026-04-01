(function(window) {
  window['env'] = window['env'] || {};
  window['env']['apiUrl'] = 'http://localhost:8085/api';
  window['env']['giteaUrl'] = 'http://localhost:3000/srw';
  window['env']['dozzleUrl'] = 'http://localhost:9999';
  window['env']['minioConsoleUrl'] = 'http://localhost:9001';
  window['env']['neo4jUrl'] = 'http://localhost:7474';
  window['env']['pgadminUrl'] = 'http://localhost:5050';
  window['env']['mongoExpressUrl'] = 'http://localhost:8081';
  window['env']['mcpUrl'] = 'http://localhost:8055/mcp';

  // Keycloak SSO
  window['env']['keycloakUrl'] = 'http://localhost:8180';
  window['env']['keycloakRealm'] = 'srw';
  window['env']['keycloakClientId'] = 'cockpit';

  // Available models for the job creation form (group + model IDs).
  window['env']['models'] = [
    { group: 'Local', models: ['openai/gpt-oss-120b'] },
    { group: 'OpenAI', models: ['gpt-5.2', 'gpt-5.2-pro'] },
    { group: 'Anthropic', models: ['claude-sonnet-4-5-20250929', 'claude-opus-4-6'] },
    { group: 'Google', models: ['gemini-2.5-pro', 'gemini-2.5-flash'] },
    { group: 'Groq', models: ['groq/moonshotai/kimi-k2-instruct-0905', 'groq/gpt-oss-120b'] },
    { group: 'OpenRouter', models: ['openrouter/minimax/minimax-m2.7'] },
  ];

  // Quick-select presets for strategic + tactical model combinations.
  window['env']['modelPresets'] = [
    { label: 'Opus + Sonnet', strategic: 'claude-opus-4-6', tactical: 'claude-sonnet-4-5-20250929' },
    { label: 'GPT-5.2 Pro + GPT-5.2', strategic: 'gpt-5.2-pro', tactical: 'gpt-5.2' },
    { label: 'Gemini Pro + Flash', strategic: 'gemini-2.5-pro', tactical: 'gemini-2.5-flash' },
    { label: 'K2 + OSS 120B (Groq)', strategic: 'groq/moonshotai/kimi-k2-instruct-0905', tactical: 'groq/gpt-oss-120b' },
    { label: 'OSS 120B Local (both)', strategic: 'openai/gpt-oss-120b', tactical: 'openai/gpt-oss-120b' },
  ];

  // Models available in the instruction builder chat.
  window['env']['builderModels'] = [
    { label: 'GPT OSS 120B (Local)', id: 'openai/gpt-oss-120b' },
    { label: 'MiniMax M2.7', id: 'openrouter/minimax/minimax-m2.7' },
    { label: 'GPT-5.2 Pro', id: 'gpt-5.2-pro' },
    { label: 'GPT-5.4 Pro (Codex)', id: 'codex/gpt-5.4-pro' },
    { label: 'Claude Opus 4.6', id: 'claude-opus-4-6' },
  ];
})(this);
