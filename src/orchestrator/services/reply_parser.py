"""Reply content parser — strips signatures, quoted text, and email cruft.

Extracts the visible reply text from an email body, removing:
- Email signatures (-- delimiter and common patterns)
- Quoted text (lines starting with >)
- Attribution lines ("On DATE, NAME wrote:")
- Forwarded message headers
- Outlook-style separators

Uses the email-reply-parser library when available, falling back to
regex-based stripping.
"""

import logging
import re

logger = logging.getLogger(__name__)

try:
    from email_reply_parser import EmailReplyParser

    _HAS_REPLY_PARSER = True
except ImportError:
    _HAS_REPLY_PARSER = False
    logger.info(
        "email-reply-parser not installed — using regex fallback for reply parsing"
    )


def parse_reply(body: str) -> str:
    """Extract the visible reply text from an email body.

    Args:
        body: Raw email body text

    Returns:
        Cleaned reply text, stripped of signatures and quotes.
        Falls back to the original body if stripping produces empty output.
    """
    if not body or not body.strip():
        return ""

    # Try library-based parsing first
    if _HAS_REPLY_PARSER:
        try:
            reply = EmailReplyParser.parse_reply(body)
            if reply and reply.strip():
                return reply.strip()
        except Exception as e:
            logger.debug(f"email-reply-parser failed, using regex fallback: {e}")

    # Fallback: regex-based stripping
    return _regex_strip(body)


# Common attribution patterns across email clients
_ATTRIBUTION_PATTERNS = [
    # English: "On Mar 18, 2026, Alice wrote:"
    re.compile(r"^On .+ wrote:\s*$", re.IGNORECASE),
    # German: "Am 18.03.2026 um 14:30 schrieb Alice:"
    re.compile(r"^Am .+ schrieb .+:\s*$", re.IGNORECASE),
    # French: "Le 18 mars 2026, Alice a écrit :"
    re.compile(r"^Le .+ a écrit\s*:\s*$", re.IGNORECASE),
    # Generic date + name + colon pattern
    re.compile(r"^\d{1,4}[./-]\d{1,2}[./-]\d{1,4}.+:\s*$"),
]

# Stop markers — lines matching these indicate the start of quoted/meta content
_STOP_PATTERNS = [
    re.compile(r"^-{5,}\s*Forwarded message\s*-{5,}"),
    re.compile(r"^_{5,}$"),  # Outlook separator
    re.compile(r"^From:\s+.+@.+", re.IGNORECASE),  # Outlook inline From: header
    re.compile(r"^Sent from my ", re.IGNORECASE),  # Mobile signatures
    re.compile(r"^Get Outlook for ", re.IGNORECASE),
]


def _regex_strip(body: str) -> str:
    """Regex-based signature/quote stripping."""
    lines = body.split("\n")
    cleaned = []

    for line in lines:
        stripped = line.strip()

        # Standard signature delimiter (RFC 3676)
        if stripped == "--" or stripped == "-- ":
            break

        # Quoted text block
        if line.startswith(">"):
            break

        # Attribution line
        if any(p.match(stripped) for p in _ATTRIBUTION_PATTERNS):
            break

        # Other stop markers
        if any(p.match(stripped) for p in _STOP_PATTERNS):
            break

        cleaned.append(line)

    # Strip trailing whitespace/empty lines
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()

    result = "\n".join(cleaned).strip()
    return result if result else body.strip()
