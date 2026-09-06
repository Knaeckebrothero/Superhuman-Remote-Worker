"""Characterize response decoding independently of either STT service's policy."""

from importlib import import_module
from types import SimpleNamespace

import pytest
from openai.types.audio import Transcription


@pytest.fixture(
    params=["agent.services.audio_helper", "orchestrator.services.transcribe"]
)
def extract_transcript(request):
    return import_module(request.param)._extract_transcript


@pytest.mark.parametrize(
    "result, expected",
    [
        pytest.param(Transcription(text="  hello\nworld  "), "hello\nworld", id="sdk"),
        pytest.param({"text": "  hello  "}, "hello", id="dict"),
        pytest.param("  hello\nworld  ", "hello\nworld", id="plain-text"),
        pytest.param(' {"text": "  hello  "} ', "hello", id="json-object"),
        pytest.param(' {"text": "hello" ', '{"text": "hello"', id="malformed-json"),
        pytest.param(' {"text": "hello",} ', '{"text": "hello",}', id="invalid-json"),
        pytest.param(
            ' {"other": "text"} ', '{"other": "text"}', id="json-missing-text"
        ),
        pytest.param(' {"other": 1} ', '{"other": 1}', id="unrelated-json"),
        pytest.param(' [{"text": "hello"}] ', '[{"text": "hello"}]', id="json-array"),
        pytest.param(' "hello" ', '"hello"', id="json-string"),
        pytest.param("", "", id="empty-text"),
        pytest.param(" \n\t ", "", id="whitespace"),
        pytest.param({}, "", id="empty-dict"),
        pytest.param({"other": "hello"}, "", id="dict-missing-text"),
        pytest.param(None, "", id="no-result"),
        pytest.param(SimpleNamespace(text=None), "", id="null-attribute"),
        pytest.param(["hello"], "", id="unsupported-result"),
        pytest.param({"text": None}, "", id="dict-null-text"),
        pytest.param('{"text": null}', "", id="json-null-text"),
        pytest.param({"text": False}, "", id="false-text"),
        pytest.param({"text": 0}, "", id="zero-text"),
        pytest.param({"text": []}, "", id="empty-list-text"),
        pytest.param({"text": b" hello "}, b"hello", id="bytes-text-is-not-coerced"),
    ],
)
def test_transcript_response_shapes(extract_transcript, result, expected):
    assert extract_transcript(result) == expected


@pytest.mark.parametrize(
    "result",
    [
        pytest.param(SimpleNamespace(text=1), id="attribute"),
        pytest.param({"text": 1}, id="dict"),
        pytest.param('{"text": 1}', id="json"),
        pytest.param({"text": ["hello"]}, id="list"),
    ],
)
def test_truthy_nontext_values_keep_the_existing_error(extract_transcript, result):
    with pytest.raises(AttributeError, match="strip"):
        extract_transcript(result)


def test_text_attribute_precedes_dictionary_entry(extract_transcript):
    class DictWithText(dict):
        text = " attribute "

    assert extract_transcript(DictWithText(text="entry")) == "attribute"
    assert extract_transcript(DictWithText(text="")) == "attribute"
