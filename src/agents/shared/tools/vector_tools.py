"""Vector search tools for workspace semantic search.

Provides LangGraph-compatible tools for semantic search over workspace files.
Uses pgvector for efficient similarity search with OpenAI embeddings.
"""

import logging
from typing import List, Optional

from langchain_core.tools import tool

from .context import ToolContext

logger = logging.getLogger(__name__)


def create_vector_tools(context: ToolContext) -> List:
    """Create vector search tools bound to a specific context.

    Args:
        context: ToolContext with vector_store

    Returns:
        List of LangChain tool functions

    Raises:
        ValueError: If context doesn't have a vector_store
    """
    if not context.has_vector_store():
        raise ValueError("ToolContext must have a vector_store for vector tools")

    vector_store = context.vector_store
    max_results = context.get_config("max_vector_results", 10)
    default_threshold = context.get_config("vector_similarity_threshold", 0.7)

    @tool
    async def semantic_search(
        query: str,
        path_filter: str = "",
        top_k: int = 0,
    ) -> str:
        """Search workspace files by meaning (semantic similarity).

        Use this for natural language queries when you're looking for
        conceptually related content, not exact text matches.

        Better than keyword search for:
        - Finding related concepts ("GoBD retention requirements")
        - Discovering relevant sections across files
        - Locating information when you don't know exact wording

        Args:
            query: Natural language query describing what you're looking for
            path_filter: Optional filter like 'notes/%' or 'chunks/%' (SQL LIKE pattern)
            top_k: Maximum results to return (0 = use default)

        Returns:
            Matching file excerpts ranked by relevance

        Example:
            semantic_search("compliance requirements for data retention")
            semantic_search("how to handle personal data", path_filter="notes/%")
        """
        try:
            k = top_k if top_k > 0 else max_results
            file_pattern = path_filter if path_filter else None

            results = await vector_store.search(
                query=query,
                top_k=k,
                threshold=default_threshold,
                file_pattern=file_pattern,
            )

            if not results:
                return f"No similar content found for: {query}"

            # Format results
            lines = [f"Found {len(results)} relevant sections:", ""]

            for i, result in enumerate(results, 1):
                similarity_pct = result["similarity"] * 100
                file_path = result["file_path"]
                chunk_idx = result["chunk_index"]
                preview = result["content_preview"]

                # Truncate preview if too long
                if len(preview) > 300:
                    preview = preview[:300] + "..."

                lines.append(f"{i}. [{file_path}] (chunk {chunk_idx}, {similarity_pct:.1f}% match)")
                lines.append(f"   {preview}")
                lines.append("")

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"semantic_search error: {e}")
            return f"Error performing semantic search: {str(e)}"

    @tool
    async def index_file_for_search(path: str) -> str:
        """Index or re-index a file for semantic search.

        Call this after writing important files that you want to
        search semantically later. Files are automatically indexed
        when written, but you can manually trigger re-indexing.

        Args:
            path: Relative path to the file (e.g., "notes/research.md")

        Returns:
            Confirmation with number of chunks indexed
        """
        try:
            # Read the file content
            workspace = context.workspace_manager
            if not workspace:
                return "Error: No workspace manager available"

            try:
                content = workspace.read_file(path)
            except FileNotFoundError:
                return f"Error: File not found: {path}"

            # Index it
            chunks_indexed = await vector_store.index_file(path, content, force_reindex=True)

            if chunks_indexed > 0:
                return f"Indexed {chunks_indexed} chunks from: {path}"
            else:
                return f"File not indexed (too short or unsupported type): {path}"

        except Exception as e:
            logger.error(f"index_file_for_search error for {path}: {e}")
            return f"Error indexing file: {str(e)}"

    @tool
    async def get_vector_index_stats() -> str:
        """Get statistics about the semantic search index.

        Returns information about how many files and chunks
        are currently indexed for this workspace.

        Returns:
            Index statistics summary
        """
        try:
            stats = await vector_store.get_stats()

            return (
                f"Vector Index Statistics\n"
                f"=======================\n"
                f"Job ID: {stats['job_id']}\n"
                f"Files indexed: {stats['file_count']}\n"
                f"Total chunks: {stats['chunk_count']}\n"
                f"Total characters: {stats['total_chars']:,}"
            )

        except Exception as e:
            logger.error(f"get_vector_index_stats error: {e}")
            return f"Error getting index stats: {str(e)}"

    return [
        semantic_search,
        index_file_for_search,
        get_vector_index_stats,
    ]


# Tool metadata for registry
VECTOR_TOOLS_METADATA = {
    "semantic_search": {
        "module": "vector_tools",
        "function": "semantic_search",
        "description": "Search workspace files by meaning using semantic similarity",
        "category": "workspace",
    },
    "index_file_for_search": {
        "module": "vector_tools",
        "function": "index_file_for_search",
        "description": "Index a file for semantic search",
        "category": "workspace",
    },
    "get_vector_index_stats": {
        "module": "vector_tools",
        "function": "get_vector_index_stats",
        "description": "Get statistics about the semantic search index",
        "category": "workspace",
    },
}
