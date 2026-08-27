"""Sanitize worker-authored text before a human or a model reads it.

Audit finding OC-05. Worker output reaches the officer and the Legate through
several doors — evidence pages, completion reports, routed message subjects and
bodies, escalation context, notification bodies — and a worker, a compromised
tool, or a prompt-injected web page it summarized can copy a credential into any
of them. Two redactors already existed and neither covered this:

* ``logging_config.redact`` has the right generic patterns but belongs to the
  log formatter. Presentation must not depend on a logging concern, and the
  formatter cannot know a caller's runtime secrets.
* ``kb_git_source.redact_git_error`` knows how to erase *known* secret values
  and URL userinfo, but evidence reads call it with no secrets, so it was only
  ever stripping userinfo.

This module is the one sanitizer for the presentation boundary, combining both:
known runtime values where the caller can supply them, generic secret shapes
where it cannot.

**It reports rather than hides.** Every call returns how many redactions were
made so the surface can tell the officer that evidence was withheld. Silently
altered evidence is worse than withheld evidence — he would judge a truncated
artifact believing it complete.

**It is not a security control on its own.** Redaction does not make worker text
trustworthy; that text is still untrusted instructions and must never be
followed. This only stops a credential riding along in something the officer was
always going to read. Nor does it change the underlying artifact: raw evidence
keeps its bytes and its checksum, and only the view is sanitized.

Deliberately conservative on false positives. A pattern broad enough to catch
"any long opaque string" would erase commit SHAs, job ids and content hashes —
the things an officer navigates by — so every pattern here is anchored to a
recognizable prefix, a key name, or a structural marker.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence
from urllib.parse import quote, quote_plus

REDACTED = "[REDACTED]"

# key: <value> / key=<value> for secret-ish key names. Anchored on the NAME, so
# `password: hunter2` goes and `job_id: 4f2a91c8` stays.
#
# Two details that were wrong on the first pass and are covered by tests:
# the separator accepts single quotes (`client_secret: 'shhh'` is as common as
# the double-quoted form), and the value may carry a `Bearer ` prefix — matching
# only up to the first space would erase the word "Bearer" and leave the token
# it introduces sitting in plain view.
_KV_SECRET = re.compile(
    r"(?i)\b(authorization|api[_-]?key|secret|client[_-]?secret|password|passwd|"
    r"token|access[_-]?key|private[_-]?key|refresh[_-]?token)"
    r"([\"']?\s*[:=]\s*[\"']?)"
    r"((?:bearer\s+)?[^\s\"',}{)]+)"
)

# Standalone shapes with a recognizable prefix or structure. Each is specific
# enough that a benign identifier cannot match it.
_STANDALONE: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),  # JWT
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),  # OpenAI / Anthropic style
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{8,}"),  # Slack
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),  # GitHub
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),  # Google API key
)

# A PEM block, header to footer, including the base64 body. Non-greedy so two
# adjacent keys are two matches rather than one span swallowing what is between.
_PEM_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
# A lone PEM header, for diagnostics that echo one line of a key rather than the
# whole block — the case kb_git_source handles by splitting known secrets.
_PEM_HEADER = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")

# scheme://user:password@host — erase the credential, keep the host so the
# officer can still tell WHICH remote failed.
_URL_USERINFO = re.compile(r"\b([a-zA-Z][\w+.\-]*://)[^\s/@]+:[^\s/@]*@")
# user:password@host for scp-style git remotes, which carry no scheme.
_SCP_PASSWORD = re.compile(r"\b[\w.\-]+:[^\s/@:]+@(?=[\w.\-]+[:/])")


@dataclass(frozen=True, slots=True)
class Redaction:
    """Sanitized text plus how much was removed.

    ``count`` exists so a surface can say "3 values withheld" instead of
    handing over quietly-shortened evidence. An officer who cannot tell the
    difference between "the worker wrote nothing here" and "we removed it" will
    judge the wrong artifact.
    """

    text: str
    count: int

    @property
    def redacted(self) -> bool:
        return self.count > 0


def _known_secret_variants(secrets: Iterable[str]) -> list[str]:
    """Every form a known secret plausibly appears in, longest first.

    Longest-first matters: redacting a short prefix first would leave the tail
    of a longer secret behind as an orphan fragment.
    """
    variants: set[str] = set()
    for secret in secrets or ():
        if not secret or len(secret) < 8:
            # Below this a "secret" is as likely to be a common word, and
            # erasing every occurrence of it would shred the text.
            continue
        variants.add(secret)
        variants.add(quote(secret, safe=""))
        variants.add(quote_plus(secret, safe=""))
        variants.update(line for line in secret.splitlines() if len(line) >= 8)
    return sorted(variants, key=len, reverse=True)


def sanitize(text: str | None, *, secrets: Sequence[str] = ()) -> Redaction:
    """Redact secret-shaped and known-secret content for presentation.

    ``secrets`` are exact runtime values the caller knows (a workspace token, a
    git credential). They are matched literally and in URL-encoded form, which
    catches values the generic patterns cannot recognize.
    """
    if not text:
        return Redaction(text or "", 0)

    result = text
    count = 0

    # Known values first: a real credential may also match a generic pattern,
    # and we would rather remove it as a known secret than depend on the shape.
    for secret in _known_secret_variants(secrets):
        if secret in result:
            count += result.count(secret)
            result = result.replace(secret, REDACTED)

    # PEM blocks before line-level patterns, so a whole key is one redaction
    # rather than a header plus a wall of surviving base64.
    result, n = _PEM_BLOCK.subn(REDACTED, result)
    count += n
    result, n = _PEM_HEADER.subn(REDACTED, result)
    count += n

    def _kv(match: re.Match[str]) -> str:
        return f"{match.group(1)}{match.group(2)}{REDACTED}"

    result, n = _KV_SECRET.subn(_kv, result)
    count += n

    for pattern in _STANDALONE:
        result, n = pattern.subn(REDACTED, result)
        count += n

    result, n = _URL_USERINFO.subn(rf"\1{REDACTED}@", result)
    count += n
    result, n = _SCP_PASSWORD.subn(f"{REDACTED}@", result)
    count += n

    return Redaction(result, count)


def sanitize_text(text: str | None, *, secrets: Sequence[str] = ()) -> str:
    """:func:`sanitize` when the caller has nowhere to report the count."""
    return sanitize(text, secrets=secrets).text


__all__ = ["REDACTED", "Redaction", "sanitize", "sanitize_text"]
