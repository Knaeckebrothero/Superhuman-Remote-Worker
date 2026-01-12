Based on this plan, extract the specific todos for {current_phase}.

Plan:
{plan_content}

List the todos as a JSON array with this format:
[
  {{"content": "Todo description", "priority": "high|medium|low"}},
  ...
]

Only include todos for {current_phase}. Be specific and actionable.
Return ONLY the JSON array, no other text.
