"""Knowledge search, mutation and projection request contracts."""

from pydantic import BaseModel, Field


class KnowledgeSearchRequest(BaseModel):
    """Request body for hybrid knowledge search."""

    query: str = Field(..., description="Search query text")
    limit: int = Field(10, ge=1, le=50, description="Max results to return")


class KnowledgeNoteUpdate(BaseModel):
    """Request body for updating a knowledge note."""

    status: str | None = Field(
        None, description="New status: active, resolved, superseded, archived"
    )
    add_tags: list[str] | None = Field(None, description="Tags to add")
    remove_tags: list[str] | None = Field(None, description="Tags to remove")


class KnowledgeMaterializeRequest(BaseModel):
    """Request body for materialising one note into the project's KB repo."""

    slug: str = Field(
        ..., description="Note id — becomes knowledge/<slug>.md in the KB repo"
    )
    content: str = Field(..., description="Fully rendered OKF note markdown")
    job_id: str | None = Field(
        None, description="Writing job UUID, for per-job commit attribution"
    )
    expected_blob_sha: str | None = Field(
        None,
        description=(
            "Compare-and-swap token: the note's blob SHA as the caller read it. "
            "When set, the write is refused (failed/precondition-failed) if the "
            "KB repo holds a different blob — or none — at the path."
        ),
    )
    retrieval_messages: list[str] | None = Field(
        None,
        description=(
            "Synthetic retrieval queries for this note. Omitted/None leaves "
            "any already-indexed value alone — OKF frontmatter carries no "
            "such field, so only an explicit caller has an opinion."
        ),
    )


class KnowledgeProjectionRequest(BaseModel):
    """Internal report of the projection leg of a canonical mutation."""

    synced: bool
    error: str | None = None
