"""Decode speech-to-text responses without depending on an SDK or service."""

import json


def extract_transcript(result: object) -> str:
    """Extract text from SDK objects, dicts, JSON-looking strings or plain text.

    Some compatible endpoints return JSON regardless of the requested response
    format. Unwrap their text field while preserving other raw string responses.
    Text values are not coerced: falsey values become empty, while nonempty
    values must support ``strip()``.
    """
    text = getattr(result, "text", None)
    if text is None:
        if isinstance(result, dict):
            text = result.get("text", "")
        elif isinstance(result, str):
            stripped = result.strip()
            if stripped.startswith("{") and '"text"' in stripped:
                try:
                    parsed = json.loads(stripped)
                except (ValueError, TypeError):
                    parsed = None
                if isinstance(parsed, dict):
                    text = parsed.get("text", stripped)
                else:
                    text = stripped
            else:
                text = stripped
        else:
            text = ""
    return (text or "").strip()
