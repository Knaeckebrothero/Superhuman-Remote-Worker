"""The image-delivery message is a carrier, not a user turn.

When a tool returns an image, the session loop appends a synthetic
``HumanMessage`` ("Image content from tool call <id>:") carrying the image as a
real provider content block, so a multimodal model can see it
(``src/services/image_content.make_multimodal_user_message``). HumanMessage is
the only type every provider accepts anywhere in a conversation — it is a
carrier choice, not a claim that the user said this.

Persisted as ``thread_messages.role = 'human'`` that carrier satisfied the
stateless run-queue's oldest-unanswered predicate verbatim
(``role = 'human' AND seq > consumed_seq`` — ``_PENDING_INPUT_SQL`` in
``src/api/turn_executor.py``). Every tool image therefore left a **phantom
queued input** behind: it sat unconsumed until the user's next real message
triggered a fresh attach, at which point the executor claimed the phantom as
``pending[0]`` (oldest first), rewound ``turn_count`` to its
``turn_number - 1``, and reopened an already-used turn number. Observed live on
dev thread dfab9ef9: the model re-answered the *previous* message, the user's
actual message waited a full turn, and the duplicate turn's rows sorted around
the newer user row in the transcript.

None of that raises. These tests pin the three properties that keep it fixed.

Issue: knowledge-base/knowledge/issues/synthetic_image_row_is_a_phantom_queued_input.md
"""

from __future__ import annotations

import re

from langchain_core.messages import HumanMessage

from agent.api.persistent_app import _db_rows_to_lc_messages, _serialize_message_row
from agent.api.turn_executor import _PENDING_INPUT_SQL
from shared.runtime.core.message_markers import PERSIST_ROLE_EVENT, PERSIST_ROLE_KEY
from agent.services.image_content import ExtractedImage, make_multimodal_user_message

MARKER = "Image content from tool call call_abc123:"


def _image_message() -> HumanMessage:
    return make_multimodal_user_message(
        MARKER, [ExtractedImage(base64_data="QUJD", mime_type="image/png")]
    )


# --------------------------------------------------------------------------
# The row the queue reads
# --------------------------------------------------------------------------


def test_image_delivery_persists_as_event_not_human():
    """The whole defect in one assertion: this row must not look like an
    unanswered user input to the run-queue."""
    assert _serialize_message_row(_image_message(), 1)["role"] == PERSIST_ROLE_EVENT


def test_the_marker_is_stamped_by_the_factory_not_the_call_site():
    """Two producers build this message (the session loop and the worker
    graph) and a third would be added without reading this file. Stamping it
    at the single factory is what makes the property unmissable."""
    assert _image_message().additional_kwargs[PERSIST_ROLE_KEY] == PERSIST_ROLE_EVENT
    assert (
        make_multimodal_user_message("text only", []).additional_kwargs[
            PERSIST_ROLE_KEY
        ]
        == PERSIST_ROLE_EVENT
    )


def test_an_event_row_without_a_delivery_is_not_a_pending_input():
    """The pending predicate admits 'event' rows ONLY when a live stateless
    delivery row backs them. A synthetic image row has none, so reclassifying
    it retires the phantom instead of renaming it.

    Asserted against the shipped SQL text: the guard is what makes the role
    flip a fix rather than a relabel, and a future edit that drops the
    delivery join would silently restore the bug.
    """
    normalized = re.sub(r"\s+", " ", _PENDING_INPUT_SQL)
    assert "message.role = 'human' AND message.seq > $2" in normalized
    assert "message.role = 'event'" in normalized
    assert "delivery.execution_lane = 'stateless'" in normalized
    assert "delivery.state IN ('persisted', 'queued', 'deferred')" in normalized


# --------------------------------------------------------------------------
# What the model sees (must not change)
# --------------------------------------------------------------------------


def test_the_model_context_is_unchanged_by_the_role():
    """'event' is a transcript/queue distinction, never a context one. If the
    restore dropped or re-typed these rows, a session would stop seeing an
    image it had already been shown — a silent regression, since the restore
    chain has no else branch."""
    row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "role": PERSIST_ROLE_EVENT,
        "content": MARKER,
        "tool_calls": None,
        "tool_call_id": None,
        "turn_number": 1,
    }
    restored = _db_rows_to_lc_messages([row])

    assert len(restored) == 1, "the image-delivery row was silently dropped"
    assert isinstance(restored[0], HumanMessage)
    assert restored[0].content == MARKER
    assert restored[0].id == row["id"]


def test_the_image_content_blocks_still_reach_the_provider():
    """The role marker rides in additional_kwargs and must not disturb the
    content list the multimodal provider actually reads."""
    msg = _image_message()
    assert msg.content[0] == {"type": "text", "text": MARKER}
    assert msg.content[1]["image_url"]["url"] == "data:image/png;base64,QUJD"


# --------------------------------------------------------------------------
# The transcript
# --------------------------------------------------------------------------


def test_a_real_user_message_is_still_a_user_row():
    """The fix must not widen: only the factory's carrier is reclassified."""
    assert _serialize_message_row(HumanMessage(content="look at this"), 1)["role"] == (
        "human"
    )
