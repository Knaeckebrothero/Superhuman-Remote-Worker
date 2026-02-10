(function(window) {
  window['env'] = window['env'] || {};
  window['env']['apiUrl'] = 'http://localhost:8085/api';
  window['env']['giteaUrl'] = 'http://localhost:3000/graphrag';
  window['env']['dozzleUrl'] = 'http://localhost:9999';

  // Available models for the job creation form (group + model IDs).
  window['env']['models'] = [
    { group: 'Local', models: ['openai/gpt-oss-120b'] },
    { group: 'OpenAI', models: ['gpt-5.2', 'gpt-5.2-pro'] },
    { group: 'Anthropic', models: ['claude-sonnet-4-5-20250929', 'claude-opus-4-6'] },
    { group: 'Google', models: ['gemini-2.5-pro', 'gemini-2.5-flash'] },
    { group: 'Groq', models: ['moonshotai/kimi-k2-instruct-0905', 'groq/gpt-oss-120b'] },
  ];

  // Quick-select presets for strategic + tactical model combinations.
  window['env']['modelPresets'] = [
    { label: 'Opus + Sonnet', strategic: 'claude-opus-4-6', tactical: 'claude-sonnet-4-5-20250929' },
    { label: 'GPT-5.2 Pro + GPT-5.2', strategic: 'gpt-5.2-pro', tactical: 'gpt-5.2' },
    { label: 'Gemini Pro + Flash', strategic: 'gemini-2.5-pro', tactical: 'gemini-2.5-flash' },
    { label: 'K2 + OSS 120B (Groq)', strategic: 'moonshotai/kimi-k2-instruct-0905', tactical: 'groq/gpt-oss-120b' },
    { label: 'OSS 120B Local (both)', strategic: 'openai/gpt-oss-120b', tactical: 'openai/gpt-oss-120b' },
  ];
})(this);
