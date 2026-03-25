"""
LLM Response Degeneration Validator.

Detects common LLM output degeneration patterns (tag loops, token repetition,
special token leakage, etc.) before the response is committed to agent state.

Each pattern is a named check with a detection function and severity level.
Add new patterns by appending to DEGENERATION_PATTERNS.
"""

import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional


@dataclass
class MatchedPattern:
    """A single matched degeneration pattern."""

    name: str
    severity: str  # "critical" | "warning"
    description: str  # Human-readable description of what was found


@dataclass
class ValidationResult:
    """Result of response validation."""

    is_degenerate: bool = False  # True if any critical pattern matched
    has_warnings: bool = False  # True if any warning pattern matched
    matched_patterns: List[MatchedPattern] = field(default_factory=list)
    truncated_content: Optional[str] = None  # Cleaned content if degeneration found


@dataclass
class DegenerationPattern:
    """A degeneration detection pattern."""

    name: str
    severity: str  # "critical" | "warning"
    detect: Callable[..., Optional[str]]
    # detect(content, **kwargs) → description string if matched, None otherwise


# ---------------------------------------------------------------------------
# Detection functions
# ---------------------------------------------------------------------------


def _detect_tag_repetition(
    content: str, max_tag_repetitions: int = 50, **_
) -> Optional[str]:
    """Detect closing XML/HTML tags repeated excessively (e.g. </invoke> spam)."""
    # Find all closing tags
    closing_tags = re.findall(r"</\w[\w:.-]*>", content)
    if not closing_tags:
        return None

    # Count occurrences of each tag
    tag_counts: dict[str, int] = {}
    for tag in closing_tags:
        tag_counts[tag] = tag_counts.get(tag, 0) + 1

    for tag, count in tag_counts.items():
        if count > max_tag_repetitions:
            return (
                f"Tag {tag} repeated {count} times (threshold: {max_tag_repetitions})"
            )
    return None


def _detect_token_repetition(
    content: str, max_token_repetitions: int = 20, **_
) -> Optional[str]:
    """Detect a single token/word repeating consecutively."""
    words = content.split()
    if len(words) < max_token_repetitions:
        return None

    streak = 1
    worst_word = ""
    worst_streak = 0

    for i in range(1, len(words)):
        if words[i] == words[i - 1]:
            streak += 1
            if streak > worst_streak:
                worst_streak = streak
                worst_word = words[i]
        else:
            streak = 1

    if worst_streak > max_token_repetitions:
        token_preview = worst_word[:40] + "..." if len(worst_word) > 40 else worst_word
        return (
            f"Token '{token_preview}' repeated {worst_streak} consecutive times "
            f"(threshold: {max_token_repetitions})"
        )
    return None


def _detect_line_repetition(
    content: str, max_line_repetitions: int = 10, **_
) -> Optional[str]:
    """Detect the same line repeating many times."""
    lines = content.splitlines()
    if len(lines) < max_line_repetitions:
        return None

    line_counts: dict[str, int] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        line_counts[stripped] = line_counts.get(stripped, 0) + 1

    for line, count in line_counts.items():
        if count > max_line_repetitions:
            line_preview = line[:60] + "..." if len(line) > 60 else line
            return (
                f"Line '{line_preview}' repeated {count} times "
                f"(threshold: {max_line_repetitions})"
            )
    return None


def _detect_special_token_leakage(content: str, **_) -> Optional[str]:
    """Detect leaked special/control tokens from training data."""
    special_patterns = [
        r"<\|endoftext\|>",
        r"<\|im_start\|>",
        r"<\|im_end\|>",
        r"<\|pad\|>",
        r"<\|sep\|>",
        r"\[INST\]",
        r"\[/INST\]",
        r"</s>",
        r"<s>",
    ]

    total_matches = 0
    found_tokens = []

    for pattern in special_patterns:
        matches = re.findall(pattern, content)
        if matches:
            total_matches += len(matches)
            found_tokens.append(f"{matches[0]} x{len(matches)}")

    if total_matches > 3:
        return f"Special token leakage detected ({total_matches} occurrences): {', '.join(found_tokens)}"
    return None


def _detect_foreign_tool_syntax(content: str, **_) -> Optional[str]:
    """Detect namespaced XML tool calls that shouldn't appear in text content."""
    pattern = r"<(?:minimax|anthropic|google|meta|cohere|mistral):[a-zA-Z_]+"
    matches = re.findall(pattern, content)
    if matches:
        unique = list(set(matches))[:5]
        return f"Foreign tool-call syntax detected: {', '.join(unique)}"
    return None


def _detect_unclosed_code_blocks(content: str, **_) -> Optional[str]:
    """Detect unclosed code fences in long content."""
    if len(content) < 5000:
        return None

    fence_count = len(re.findall(r"^```", content, re.MULTILINE))
    if fence_count % 2 != 0:
        return f"Unclosed code block detected ({fence_count} fence markers in {len(content)} chars)"
    return None


def _detect_excessive_length(
    content: str,
    tool_calls: Optional[list] = None,
    max_content_length: int = 50000,
    **_,
) -> Optional[str]:
    """Flag excessive text content with no tool calls."""
    has_tool_calls = bool(tool_calls)
    if not has_tool_calls and len(content) > max_content_length:
        return (
            f"Response is {len(content)} chars with no tool calls "
            f"(threshold: {max_content_length})"
        )
    return None


# ---------------------------------------------------------------------------
# Pattern registry
# ---------------------------------------------------------------------------

DEGENERATION_PATTERNS: List[DegenerationPattern] = [
    DegenerationPattern(
        name="tag_repetition_loop",
        severity="critical",
        detect=_detect_tag_repetition,
    ),
    DegenerationPattern(
        name="token_repetition",
        severity="critical",
        detect=_detect_token_repetition,
    ),
    DegenerationPattern(
        name="phrase_repetition",
        severity="critical",
        detect=_detect_line_repetition,
    ),
    DegenerationPattern(
        name="special_token_leakage",
        severity="warning",
        detect=_detect_special_token_leakage,
    ),
    DegenerationPattern(
        name="foreign_tool_syntax",
        severity="critical",
        detect=_detect_foreign_tool_syntax,
    ),
    DegenerationPattern(
        name="unclosed_code_blocks",
        severity="warning",
        detect=_detect_unclosed_code_blocks,
    ),
    DegenerationPattern(
        name="excessive_length",
        severity="warning",
        detect=_detect_excessive_length,
    ),
]


# ---------------------------------------------------------------------------
# Main validation entry point
# ---------------------------------------------------------------------------


def validate_response(
    content: str,
    tool_calls: Optional[list] = None,
    *,
    max_content_length: int = 50000,
    max_tag_repetitions: int = 50,
    max_token_repetitions: int = 20,
    max_line_repetitions: int = 10,
) -> ValidationResult:
    """Validate an LLM response for degeneration patterns.

    Args:
        content: The text content of the LLM response.
        tool_calls: List of tool calls from the response (if any).
        max_content_length: Threshold for excessive length warning.
        max_tag_repetitions: Threshold for tag repetition detection.
        max_token_repetitions: Threshold for consecutive token repetition.
        max_line_repetitions: Threshold for line repetition detection.

    Returns:
        ValidationResult with matched patterns and degeneration status.
    """
    if not content:
        return ValidationResult()

    kwargs = {
        "tool_calls": tool_calls,
        "max_content_length": max_content_length,
        "max_tag_repetitions": max_tag_repetitions,
        "max_token_repetitions": max_token_repetitions,
        "max_line_repetitions": max_line_repetitions,
    }

    result = ValidationResult()

    for pattern in DEGENERATION_PATTERNS:
        description = pattern.detect(content, **kwargs)
        if description is not None:
            result.matched_patterns.append(
                MatchedPattern(
                    name=pattern.name,
                    severity=pattern.severity,
                    description=description,
                )
            )
            if pattern.severity == "critical":
                result.is_degenerate = True
            elif pattern.severity == "warning":
                result.has_warnings = True

    # Generate truncated content preview for critical matches
    if result.is_degenerate:
        # Show first 500 chars + last 200 chars for context
        if len(content) > 800:
            result.truncated_content = (
                content[:500] + "\n...[truncated]...\n" + content[-200:]
            )
        else:
            result.truncated_content = content

    return result
