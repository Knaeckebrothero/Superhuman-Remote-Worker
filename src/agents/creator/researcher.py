"""Researcher Component for Creator Agent.

Implements research capabilities including web search (Tavily),
graph queries for similar requirements, and context enrichment.
"""

import os
import logging
import re
from typing import Dict, Any, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class Researcher:
    """Research component for enriching requirement candidates.

    Provides capabilities for:
    - Web search using Tavily API
    - Graph queries for similar requirements
    - Context gathering and enrichment
    - Research note compilation
    """

    def __init__(
        self,
        web_search_enabled: bool = True,
        graph_search_enabled: bool = True,
        neo4j_conn: Optional[Any] = None
    ):
        """Initialize the researcher.

        Args:
            web_search_enabled: Enable web search capability
            graph_search_enabled: Enable graph search capability
            neo4j_conn: Neo4j connection for graph queries
        """
        self.web_search_enabled = web_search_enabled
        self.graph_search_enabled = graph_search_enabled
        self.neo4j_conn = neo4j_conn

        # Initialize Tavily client if available
        self._tavily_client = None
        if web_search_enabled:
            self._init_tavily()

    def _init_tavily(self) -> None:
        """Initialize Tavily search client."""
        try:
            api_key = os.getenv("TAVILY_API_KEY")
            if api_key:
                from langchain_tavily import TavilySearchResults
                self._tavily_client = TavilySearchResults(
                    api_key=api_key,
                    max_results=5,
                    search_depth="advanced",
                )
                logger.info("Tavily search client initialized")
            else:
                logger.warning("TAVILY_API_KEY not set - web search disabled")
                self.web_search_enabled = False
        except ImportError:
            logger.warning("langchain-tavily not installed - web search disabled")
            self.web_search_enabled = False

    def web_search(
        self,
        query: str,
        max_results: int = 5
    ) -> List[Dict[str, Any]]:
        """Search the web using Tavily.

        Args:
            query: Search query
            max_results: Maximum results to return

        Returns:
            List of search results with url, title, snippet
        """
        if not self.web_search_enabled or not self._tavily_client:
            logger.warning("Web search not available")
            return []

        try:
            # Execute search
            results = self._tavily_client.invoke({"query": query})

            # Process results
            processed = []
            for result in results[:max_results]:
                processed.append({
                    "url": result.get("url", ""),
                    "title": result.get("title", "Untitled"),
                    "snippet": result.get("content", "")[:500],
                    "score": result.get("score", 0),
                    "retrieved_at": datetime.utcnow().isoformat(),
                })

            logger.debug(f"Web search returned {len(processed)} results for: {query}")
            return processed

        except Exception as e:
            logger.error(f"Web search error: {e}")
            return []

    def find_similar_requirements(
        self,
        text: str,
        limit: int = 5
    ) -> List[Dict[str, Any]]:
        """Find similar requirements in the Neo4j graph.

        Uses text similarity matching against existing requirements.

        Args:
            text: Requirement text to match
            limit: Maximum results

        Returns:
            List of similar requirements with similarity scores
        """
        if not self.graph_search_enabled or not self.neo4j_conn:
            logger.debug("Graph search not available")
            return []

        try:
            # Extract key terms for matching
            keywords = self._extract_keywords(text)

            if not keywords:
                return []

            # Build Cypher query for text matching
            # Note: For production, this should use a proper text search index
            keyword_patterns = "|".join(keywords[:10])

            query = """
            MATCH (r:Requirement)
            WHERE r.text =~ $pattern OR r.name =~ $pattern
            WITH r,
                 size([kw IN $keywords WHERE r.text =~ ('(?i).*' + kw + '.*')]) as matches
            WHERE matches > 0
            RETURN r.rid as rid,
                   r.name as name,
                   r.text as text,
                   r.status as status,
                   r.type as type,
                   r.goBDRelevant as gobd_relevant,
                   toFloat(matches) / toFloat($total_keywords) as similarity
            ORDER BY similarity DESC
            LIMIT $limit
            """

            # Execute query
            with self.neo4j_conn.driver.session() as session:
                result = session.run(
                    query,
                    pattern=f"(?i).*({keyword_patterns}).*",
                    keywords=keywords,
                    total_keywords=len(keywords),
                    limit=limit
                )

                similar = []
                for record in result:
                    similar.append({
                        "rid": record["rid"],
                        "name": record["name"],
                        "text": record["text"],
                        "status": record["status"],
                        "type": record["type"],
                        "gobd_relevant": record["gobd_relevant"],
                        "similarity": record["similarity"],
                    })

                logger.debug(f"Found {len(similar)} similar requirements")
                return similar

        except Exception as e:
            logger.error(f"Graph search error: {e}")
            return []

    def _extract_keywords(self, text: str) -> List[str]:
        """Extract keywords from text for search.

        Args:
            text: Text to extract keywords from

        Returns:
            List of keywords
        """
        # Remove common words
        stopwords = {
            "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
            "have", "has", "had", "do", "does", "did", "will", "would", "could",
            "should", "may", "might", "must", "shall", "can", "need", "to", "of",
            "in", "for", "on", "with", "at", "by", "from", "as", "or", "and",
            "but", "if", "then", "else", "when", "where", "that", "this", "these",
            "those", "it", "its", "they", "their", "we", "our", "you", "your",
            # German stopwords
            "der", "die", "das", "den", "dem", "des", "ein", "eine", "einer",
            "eines", "einem", "und", "oder", "aber", "wenn", "dann", "nicht",
            "ist", "sind", "war", "waren", "sein", "haben", "hat", "wird",
            "werden", "wurde", "wurden", "kann", "können", "muss", "müssen",
            "soll", "sollen", "für", "mit", "von", "zu", "nach", "bei", "aus",
        }

        # Tokenize
        words = re.findall(r'\b\w{3,}\b', text.lower())

        # Filter stopwords and get unique keywords
        keywords = [w for w in words if w not in stopwords]
        unique_keywords = list(dict.fromkeys(keywords))  # Preserve order, remove dupes

        return unique_keywords[:20]  # Limit to top 20

    def research_candidate(
        self,
        candidate: Dict[str, Any],
        research_depth: str = "standard"
    ) -> Dict[str, Any]:
        """Research a candidate requirement for context.

        Args:
            candidate: Candidate dictionary
            research_depth: 'quick', 'standard', or 'deep'

        Returns:
            Research results dictionary
        """
        results = {
            "candidate_id": candidate.get("candidate_id"),
            "web_results": [],
            "similar_requirements": [],
            "research_notes": [],
            "research_depth": research_depth,
            "researched_at": datetime.utcnow().isoformat(),
        }

        text = candidate.get("text", "")

        # Determine research scope based on depth
        web_queries = 1 if research_depth == "quick" else (2 if research_depth == "standard" else 3)
        similar_limit = 3 if research_depth == "quick" else (5 if research_depth == "standard" else 10)

        # Web search
        if self.web_search_enabled and text:
            # Build search queries
            queries = self._build_search_queries(candidate, web_queries)

            for query in queries:
                web_results = self.web_search(query, max_results=3)
                results["web_results"].extend(web_results)

        # Graph search for similar requirements
        if self.graph_search_enabled:
            similar = self.find_similar_requirements(text, limit=similar_limit)
            results["similar_requirements"] = similar

        # Generate research notes
        results["research_notes"] = self._compile_research_notes(results)

        return results

    def _build_search_queries(
        self,
        candidate: Dict[str, Any],
        num_queries: int
    ) -> List[str]:
        """Build search queries for a candidate.

        Args:
            candidate: Candidate dictionary
            num_queries: Number of queries to generate

        Returns:
            List of search query strings
        """
        queries = []
        text = candidate.get("text", "")

        # Base query: key terms from requirement
        keywords = self._extract_keywords(text)[:5]
        if keywords:
            queries.append(" ".join(keywords))

        if num_queries > 1:
            # Add domain context
            if candidate.get("gobd_relevant"):
                queries.append(f"GoBD compliance {' '.join(keywords[:3])}")
            if candidate.get("gdpr_relevant"):
                queries.append(f"GDPR requirements {' '.join(keywords[:3])}")

        if num_queries > 2:
            # Add specific object/message context
            objects = candidate.get("mentioned_objects", [])
            if objects:
                queries.append(f"car rental {objects[0]} requirements")

        return queries[:num_queries]

    def _compile_research_notes(self, results: Dict[str, Any]) -> List[str]:
        """Compile research notes from results.

        Args:
            results: Research results dictionary

        Returns:
            List of research note strings
        """
        notes = []

        # Web search notes
        web_count = len(results.get("web_results", []))
        if web_count > 0:
            notes.append(f"Found {web_count} relevant web sources")

            # Extract key insights from web results
            for result in results.get("web_results", [])[:3]:
                snippet = result.get("snippet", "")[:200]
                if snippet:
                    notes.append(f"Web insight: {snippet}...")

        # Similar requirements notes
        similar = results.get("similar_requirements", [])
        if similar:
            notes.append(f"Found {len(similar)} similar requirements in graph")

            # Note high-similarity matches
            high_sim = [r for r in similar if r.get("similarity", 0) > 0.7]
            if high_sim:
                notes.append(f"Warning: {len(high_sim)} potential duplicates detected")
                for req in high_sim[:2]:
                    notes.append(f"  - {req.get('rid', 'N/A')}: {req.get('name', 'Unnamed')} ({req.get('similarity', 0):.0%} similar)")

        if not notes:
            notes.append("No additional research context found")

        return notes

    def determine_research_depth(self, candidate: Dict[str, Any]) -> str:
        """Determine appropriate research depth for a candidate.

        Args:
            candidate: Candidate dictionary

        Returns:
            Research depth: 'quick', 'standard', or 'deep'
        """
        confidence = candidate.get("confidence", 0.5)

        # High confidence = less research needed
        if confidence >= 0.85:
            return "quick"

        # Complex or compliance requirements need more research
        if candidate.get("gobd_relevant") or candidate.get("gdpr_relevant"):
            return "deep"

        if candidate.get("type") == "compliance":
            return "deep"

        # Check for complexity indicators
        text = candidate.get("text", "")
        complexity_indicators = [
            "integration", "interface", "multiple", "all",
            "system", "database", "audit", "security",
        ]
        if any(ind in text.lower() for ind in complexity_indicators):
            return "standard"

        return "quick" if confidence >= 0.7 else "standard"


# =============================================================================
# Factory Function
# =============================================================================

def create_researcher(
    web_search_enabled: bool = True,
    graph_search_enabled: bool = True,
    neo4j_conn: Optional[Any] = None
) -> Researcher:
    """Create a researcher instance.

    Args:
        web_search_enabled: Enable web search
        graph_search_enabled: Enable graph search
        neo4j_conn: Neo4j connection

    Returns:
        Configured Researcher
    """
    return Researcher(
        web_search_enabled=web_search_enabled,
        graph_search_enabled=graph_search_enabled,
        neo4j_conn=neo4j_conn
    )
