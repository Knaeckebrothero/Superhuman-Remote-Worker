"""Bounded JSON and YAML inputs using one JSON-compatible value model."""

import json
import re

import yaml

from .errors import ManifestError, fail
from .validation import MAX_DOCUMENTS, MAX_SOURCE_BYTES, validate_documents


class ManifestLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                fail("YAMLMergeKey", "YAML merge keys are not supported.")
            key = self.construct_object(key_node, deep=deep)
            if type(key) is not str:
                fail("InvalidObjectKey", "Object keys must be strings.")
            if key in result:
                fail("DuplicateKey", "Duplicate object key.")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


_JSON_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
_NUMERIC = re.compile(
    r"[+-]?(?:[0-9][0-9_]*(?:\.[0-9_]*)?(?:[eE][+-]?[0-9_]+)?|0[xXoObB][0-9a-fA-F_]+|[0-9_]+(?::[0-9_.]+)+)\Z"
)


def _scalar(loader, node):
    value = loader.construct_scalar(node)
    kind = node.tag.rsplit(":", 1)[-1]
    accepted = (
        (kind == "bool" and value in ("true", "false"))
        or (kind == "null" and value == "null")
        or (kind in ("int", "float") and _JSON_NUMBER.fullmatch(value))
    )
    if not accepted:
        fail("AmbiguousScalar", "Use JSON scalar spellings or quote the value as text.")
    return json.loads(value)


for _kind in ("bool", "null", "int", "float"):
    ManifestLoader.add_constructor(f"tag:yaml.org,2002:{_kind}", _scalar)
ManifestLoader.add_implicit_resolver(
    "tag:yaml.org,2002:float", _NUMERIC, list("-+0123456789")
)


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            fail("DuplicateKey", "Duplicate object key.")
        result[key] = value
    return result


def _constant(value):
    fail("InvalidJSONValue", "JSON numbers must be finite.")


def parse_documents(source: str, *, format: str = "yaml") -> list[dict]:
    if not isinstance(source, str):
        fail("InvalidSource", "Manifest source must be text.")
    try:
        size = len(source.encode("utf-8"))
    except UnicodeError:
        fail("InvalidSource", "Manifest source must be valid UTF-8 text.")
    if size > MAX_SOURCE_BYTES:
        fail("InputLimitExceeded", "Manifest text exceeds the 1 MiB limit.")
    if format not in ("json", "yaml"):
        fail("InvalidFormat", "Choose json or yaml.")
    documents = []
    try:
        if format == "json":
            value = json.loads(
                source, object_pairs_hook=_json_object, parse_constant=_constant
            )
            documents = value if type(value) is list else [value]
        else:
            for document in yaml.load_all(source, Loader=ManifestLoader):
                if len(documents) == MAX_DOCUMENTS:
                    fail("DocumentLimit", "Too many manifest documents.")
                documents.append(document)
    except ManifestError as exc:
        if format == "yaml":
            fail(
                exc.issue.code,
                exc.issue.message,
                document=len(documents) + 1,
                path=exc.issue.path,
            )
        raise
    except (ValueError, yaml.YAMLError, RecursionError, OverflowError):
        # Parser exceptions may contain whole source lines, including secrets.
        fail(
            "InvalidSyntax",
            "Unable to parse manifest source.",
            document=len(documents) + 1,
        )
    return validate_documents(documents)
