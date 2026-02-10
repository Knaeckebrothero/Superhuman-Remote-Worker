(function(window) {
  window['env'] = window['env'] || {};
  window['env']['apiUrl'] = 'http://localhost:8085/api';
  window['env']['giteaUrl'] = 'http://localhost:3000/graphrag';
  window['env']['dozzleUrl'] = 'http://localhost:9999';

  // Available models for the job creation form (group + model IDs).
  window['env']['models'] = [
    { group: 'Custom Endpoint', models: ['openai/gpt-oss-120b'] },
    { group: 'OpenAI', models: ['gpt-5.2', 'gpt-5.2-pro'] },
    { group: 'Anthropic', models: ['claude-sonnet-4-5-20250929', 'claude-opus-4-6'] },
    { group: 'Google', models: ['gemini-2.5-pro', 'gemini-2.5-flash'] },
    { group: 'Groq', models: ['moonshotai/kimi-k2-instruct-0905'] },
  ];
})(this);
