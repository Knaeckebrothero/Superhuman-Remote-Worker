"""Guard: the agent pre-auth key must be ephemeral, or agent nodes leak.

See docs/superpowers/specs/2026-06-04-headscale-agent-ephemeral-keys-design.md.
"""

import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "headscale-bootstrap.sh"


def test_preauthkey_creation_is_ephemeral():
    text = SCRIPT.read_text()
    # Isolate the `headscale preauthkeys create ...` invocation.
    m = re.search(r"headscale preauthkeys create.*?--tags", text, re.DOTALL)
    assert m, "could not find the preauthkeys create block"
    block = m.group(0)
    assert "--reusable" in block, "agent key must stay reusable (shared key)"
    assert "--ephemeral" in block, "agent key MUST be --ephemeral or agent nodes leak"
