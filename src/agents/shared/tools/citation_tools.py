"""Citation tools for the Universal Agent.

Provides document and web citation capabilities for requirement traceability.
"""

import logging
import uuid
from datetime import datetime
from typing import List, Optional

from langchain_core.tools import tool

from .context import ToolContext

logger = logging.getLogger(__name__)


# Tool metadata for registry
CITATION_TOOLS_METADATA = {
    "cite_document": {
        "module": "citation_tools",
        "function": "cite_document",
        "description": "Create a citation for document content",
        "category": "domain",
    },
    "cite_web": {
        "module": "citation_tools",
        "function": "cite_web",
        "description": "Create a citation for web content",
        "category": "domain",
    },
}


def create_citation_tools(context: ToolContext) -> List:
    """Create citation tools with injected context.

    Args:
        context: ToolContext with dependencies

    Returns:
        List of LangChain tool functions
    """

    @tool
    def cite_document(
        text: str,
        document_path: str,
        page: Optional[int] = None,
        section: Optional[str] = None
    ) -> str:
        """Create a citation for document content.

        Args:
            text: Quoted text from the document
            document_path: Source document path
            page: Page number if applicable
            section: Section reference if applicable

        Returns:
            Citation ID and details
        """
        try:
            # Generate citation ID
            citation_id = f"CIT-{uuid.uuid4().hex[:8].upper()}"

            # Store citation if database available
            if context.postgres_conn and context.job_id:
                try:
                    # In full implementation, store to citations table
                    pass
                except Exception as e:
                    logger.warning(f"Could not store citation: {e}")

            result = f"""Citation Created

Citation ID: {citation_id}
Source Type: Document
Document: {document_path}
Page: {page or 'N/A'}
Section: {section or 'N/A'}

Quoted Text:
"{text[:500]}{"..." if len(text) > 500 else ""}"

Use this citation_id when writing requirements."""

            return result

        except Exception as e:
            logger.error(f"Citation creation error: {e}")
            return f"Error creating citation: {str(e)}"

    @tool
    def cite_web(
        text: str,
        url: str,
        title: Optional[str] = None,
        accessed_date: Optional[str] = None
    ) -> str:
        """Create a citation for web content.

        Args:
            text: Quoted/paraphrased text from the web
            url: Source URL
            title: Page title
            accessed_date: Date accessed (ISO format)

        Returns:
            Citation ID and details
        """
        try:
            citation_id = f"CIT-{uuid.uuid4().hex[:8].upper()}"

            # Use today's date if not provided
            if not accessed_date:
                accessed_date = datetime.utcnow().strftime('%Y-%m-%d')

            # Store citation if database available
            if context.postgres_conn and context.job_id:
                try:
                    # In full implementation, store to citations table
                    pass
                except Exception as e:
                    logger.warning(f"Could not store citation: {e}")

            result = f"""Web Citation Created

Citation ID: {citation_id}
Source Type: Web
URL: {url}
Title: {title or 'Untitled'}
Accessed: {accessed_date}

Content:
"{text[:500]}{"..." if len(text) > 500 else ""}"

Use this citation_id when writing requirements."""

            return result

        except Exception as e:
            logger.error(f"Web citation error: {e}")
            return f"Error creating web citation: {str(e)}"

    return [
        cite_document,
        cite_web,
    ]
