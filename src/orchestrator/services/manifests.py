"""Read-only authored-bundle operations, without access to live resource stores."""

from shared.manifests import (
    API_VERSION,
    export_documents,
    load_schema,
    parse_documents,
    preview_documents,
)


class ManifestService:
    def schema(self) -> dict:
        return load_schema()

    def validate(self, source: str, *, format: str = "yaml") -> dict:
        documents = parse_documents(source, format=format)
        return {
            "apiVersion": API_VERSION,
            "valid": True,
            "documents": [
                {"kind": doc["kind"], "name": doc["metadata"]["name"]}
                for doc in documents
            ],
            "checks": ["syntax", "schema", "projectAliases"],
        }

    def preview(
        self, source: str, *, format: str = "yaml", default_scope: dict | None = None
    ) -> dict:
        return preview_documents(
            parse_documents(source, format=format), default_scope=default_scope
        )

    def export(
        self,
        source: str,
        *,
        format: str = "yaml",
        default_scope: dict | None = None,
        output_format: str = "yaml",
    ) -> dict:
        return {
            "apiVersion": API_VERSION,
            "format": output_format,
            "source": export_documents(
                parse_documents(source, format=format),
                default_scope=default_scope,
                format=output_format,
            ),
        }
