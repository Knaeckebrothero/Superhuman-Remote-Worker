"""HTTP client for communicating with Creator and Validator agents.

This module provides a unified interface for the Streamlit dashboard to
communicate with the deployed agent containers via HTTP.
"""

import os
import base64
import asyncio
import logging
from dataclasses import dataclass
from typing import Optional, Dict, Any, List
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)


@dataclass
class AgentConfig:
    """Configuration for agent connections."""

    creator_url: str = "http://localhost:8001"
    validator_url: str = "http://localhost:8002"
    timeout: float = 30.0

    @classmethod
    def from_env(cls) -> "AgentConfig":
        """Load configuration from environment variables."""
        return cls(
            creator_url=os.getenv("CREATOR_AGENT_URL", "http://localhost:8001"),
            validator_url=os.getenv("VALIDATOR_AGENT_URL", "http://localhost:8002"),
            timeout=float(os.getenv("AGENT_TIMEOUT", "30.0")),
        )


@dataclass
class ServiceHealth:
    """Health status of a service."""

    name: str
    url: str
    healthy: bool
    message: Optional[str] = None
    uptime_seconds: Optional[float] = None
    state: Optional[str] = None
    current_job_id: Optional[str] = None


class AgentClient:
    """HTTP client for Creator and Validator agent APIs."""

    def __init__(self, config: Optional[AgentConfig] = None):
        """Initialize the agent client.

        Args:
            config: Agent configuration. If None, loads from environment.
        """
        self.config = config or AgentConfig.from_env()
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        """Async context manager entry."""
        self._client = httpx.AsyncClient(timeout=self.config.timeout)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self._client:
            await self._client.aclose()

    def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.config.timeout)
        return self._client

    # =========================================================================
    # Health Checks
    # =========================================================================

    async def check_creator_health(self) -> ServiceHealth:
        """Check Creator agent health."""
        return await self._check_health("Creator", self.config.creator_url)

    async def check_validator_health(self) -> ServiceHealth:
        """Check Validator agent health."""
        return await self._check_health("Validator", self.config.validator_url)

    async def _check_health(self, name: str, base_url: str) -> ServiceHealth:
        """Check health of a service."""
        client = self._get_client()
        try:
            # Try health endpoint
            response = await client.get(f"{base_url}/health")
            if response.status_code == 200:
                data = response.json()

                # Also get status for more details
                try:
                    status_response = await client.get(f"{base_url}/status")
                    if status_response.status_code == 200:
                        status_data = status_response.json()
                        return ServiceHealth(
                            name=name,
                            url=base_url,
                            healthy=True,
                            message="Healthy",
                            uptime_seconds=data.get("uptime_seconds"),
                            state=status_data.get("state"),
                            current_job_id=status_data.get("current_job_id"),
                        )
                except Exception:
                    pass

                return ServiceHealth(
                    name=name,
                    url=base_url,
                    healthy=True,
                    message="Healthy",
                    uptime_seconds=data.get("uptime_seconds"),
                )
            else:
                return ServiceHealth(
                    name=name,
                    url=base_url,
                    healthy=False,
                    message=f"HTTP {response.status_code}",
                )
        except httpx.ConnectError:
            return ServiceHealth(
                name=name,
                url=base_url,
                healthy=False,
                message="Connection refused",
            )
        except httpx.TimeoutException:
            return ServiceHealth(
                name=name,
                url=base_url,
                healthy=False,
                message="Connection timeout",
            )
        except Exception as e:
            return ServiceHealth(
                name=name,
                url=base_url,
                healthy=False,
                message=str(e),
            )

    async def check_all_services(self) -> Dict[str, ServiceHealth]:
        """Check health of all services."""
        creator_health, validator_health = await asyncio.gather(
            self.check_creator_health(),
            self.check_validator_health(),
            return_exceptions=True,
        )

        results = {}

        if isinstance(creator_health, Exception):
            results["creator"] = ServiceHealth(
                name="Creator",
                url=self.config.creator_url,
                healthy=False,
                message=str(creator_health),
            )
        else:
            results["creator"] = creator_health

        if isinstance(validator_health, Exception):
            results["validator"] = ServiceHealth(
                name="Validator",
                url=self.config.validator_url,
                healthy=False,
                message=str(validator_health),
            )
        else:
            results["validator"] = validator_health

        return results

    # =========================================================================
    # Creator Agent Operations
    # =========================================================================

    async def submit_job(
        self,
        prompt: str,
        document_bytes: Optional[bytes] = None,
        document_filename: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        max_iterations: int = 50,
    ) -> Dict[str, Any]:
        """Submit a job to the Creator agent.

        Args:
            prompt: Processing prompt
            document_bytes: Document content as bytes
            document_filename: Original filename
            context: Additional context dictionary
            max_iterations: Maximum processing iterations

        Returns:
            Response with job_id and status
        """
        client = self._get_client()

        payload = {
            "prompt": prompt,
            "context": context or {},
            "max_iterations": max_iterations,
        }

        if document_bytes and document_filename:
            payload["document_content"] = base64.b64encode(document_bytes).decode("utf-8")
            payload["document_filename"] = document_filename

        response = await client.post(
            f"{self.config.creator_url}/jobs",
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def get_job_status(self, job_id: str) -> Dict[str, Any]:
        """Get the status of a Creator job.

        Args:
            job_id: Job UUID

        Returns:
            Job status response
        """
        client = self._get_client()
        response = await client.get(f"{self.config.creator_url}/jobs/{job_id}")
        response.raise_for_status()
        return response.json()

    async def get_job_requirements(self, job_id: str) -> Dict[str, Any]:
        """Get requirements created by a job.

        Args:
            job_id: Job UUID

        Returns:
            List of requirements
        """
        client = self._get_client()
        response = await client.get(
            f"{self.config.creator_url}/jobs/{job_id}/requirements"
        )
        response.raise_for_status()
        return response.json()

    # =========================================================================
    # Validator Agent Operations
    # =========================================================================

    async def submit_validation(
        self,
        requirement_id: Optional[str] = None,
        text: Optional[str] = None,
        name: Optional[str] = None,
        req_type: Optional[str] = None,
        priority: Optional[str] = None,
        mentioned_objects: Optional[List[str]] = None,
        mentioned_messages: Optional[List[str]] = None,
        gobd_relevant: bool = False,
        gdpr_relevant: bool = False,
        max_iterations: int = 100,
    ) -> Dict[str, Any]:
        """Submit a requirement for validation.

        Args:
            requirement_id: Existing requirement ID to validate
            text: Requirement text for ad-hoc validation
            name: Requirement name
            req_type: Requirement type
            priority: Priority level
            mentioned_objects: Business objects mentioned
            mentioned_messages: Messages mentioned
            gobd_relevant: GoBD compliance relevance
            gdpr_relevant: GDPR compliance relevance
            max_iterations: Maximum validation iterations

        Returns:
            Response with request_id for polling
        """
        client = self._get_client()

        payload = {
            "max_iterations": max_iterations,
            "gobd_relevant": gobd_relevant,
            "gdpr_relevant": gdpr_relevant,
        }

        if requirement_id:
            payload["requirement_id"] = requirement_id
        else:
            payload["text"] = text
            if name:
                payload["name"] = name
            if req_type:
                payload["type"] = req_type
            if priority:
                payload["priority"] = priority
            if mentioned_objects:
                payload["mentioned_objects"] = mentioned_objects
            if mentioned_messages:
                payload["mentioned_messages"] = mentioned_messages

        response = await client.post(
            f"{self.config.validator_url}/validate",
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def get_validation_status(self, request_id: str) -> Dict[str, Any]:
        """Get the status of a validation request.

        Args:
            request_id: Validation request ID

        Returns:
            Validation status response
        """
        client = self._get_client()
        response = await client.get(
            f"{self.config.validator_url}/validate/{request_id}"
        )
        response.raise_for_status()
        return response.json()

    async def list_pending_requirements(self, limit: int = 50) -> Dict[str, Any]:
        """List pending requirements from the cache.

        Args:
            limit: Maximum number of requirements to return

        Returns:
            List of pending requirements
        """
        client = self._get_client()
        response = await client.get(
            f"{self.config.validator_url}/requirements/pending",
            params={"limit": limit},
        )
        response.raise_for_status()
        return response.json()

    async def get_requirement(self, requirement_id: str) -> Dict[str, Any]:
        """Get details of a specific requirement.

        Args:
            requirement_id: Requirement UUID

        Returns:
            Requirement details
        """
        client = self._get_client()
        response = await client.get(
            f"{self.config.validator_url}/requirements/{requirement_id}"
        )
        response.raise_for_status()
        return response.json()


# =============================================================================
# Synchronous Wrapper for Streamlit
# =============================================================================


def run_async(coro):
    """Run an async coroutine synchronously.

    Streamlit runs in a synchronous context, so we need to wrap
    async operations. This handles the event loop properly.
    """
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    return loop.run_until_complete(coro)


class SyncAgentClient:
    """Synchronous wrapper around AgentClient for Streamlit.

    Provides the same interface as AgentClient but with synchronous methods.
    """

    def __init__(self, config: Optional[AgentConfig] = None):
        """Initialize the synchronous client.

        Args:
            config: Agent configuration. If None, loads from environment.
        """
        self.config = config or AgentConfig.from_env()
        self._async_client = AgentClient(config=self.config)

    # Health checks
    def check_creator_health(self) -> ServiceHealth:
        """Check Creator agent health."""
        return run_async(self._async_client.check_creator_health())

    def check_validator_health(self) -> ServiceHealth:
        """Check Validator agent health."""
        return run_async(self._async_client.check_validator_health())

    def check_all_services(self) -> Dict[str, ServiceHealth]:
        """Check health of all services."""
        return run_async(self._async_client.check_all_services())

    # Creator operations
    def submit_job(
        self,
        prompt: str,
        document_bytes: Optional[bytes] = None,
        document_filename: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        max_iterations: int = 50,
    ) -> Dict[str, Any]:
        """Submit a job to the Creator agent."""
        return run_async(
            self._async_client.submit_job(
                prompt=prompt,
                document_bytes=document_bytes,
                document_filename=document_filename,
                context=context,
                max_iterations=max_iterations,
            )
        )

    def get_job_status(self, job_id: str) -> Dict[str, Any]:
        """Get the status of a Creator job."""
        return run_async(self._async_client.get_job_status(job_id))

    def get_job_requirements(self, job_id: str) -> Dict[str, Any]:
        """Get requirements created by a job."""
        return run_async(self._async_client.get_job_requirements(job_id))

    # Validator operations
    def submit_validation(
        self,
        requirement_id: Optional[str] = None,
        text: Optional[str] = None,
        name: Optional[str] = None,
        req_type: Optional[str] = None,
        priority: Optional[str] = None,
        mentioned_objects: Optional[List[str]] = None,
        mentioned_messages: Optional[List[str]] = None,
        gobd_relevant: bool = False,
        gdpr_relevant: bool = False,
        max_iterations: int = 100,
    ) -> Dict[str, Any]:
        """Submit a requirement for validation."""
        return run_async(
            self._async_client.submit_validation(
                requirement_id=requirement_id,
                text=text,
                name=name,
                req_type=req_type,
                priority=priority,
                mentioned_objects=mentioned_objects,
                mentioned_messages=mentioned_messages,
                gobd_relevant=gobd_relevant,
                gdpr_relevant=gdpr_relevant,
                max_iterations=max_iterations,
            )
        )

    def get_validation_status(self, request_id: str) -> Dict[str, Any]:
        """Get the status of a validation request."""
        return run_async(self._async_client.get_validation_status(request_id))

    def list_pending_requirements(self, limit: int = 50) -> Dict[str, Any]:
        """List pending requirements from the cache."""
        return run_async(self._async_client.list_pending_requirements(limit))

    def get_requirement(self, requirement_id: str) -> Dict[str, Any]:
        """Get details of a specific requirement."""
        return run_async(self._async_client.get_requirement(requirement_id))


def get_agent_client() -> SyncAgentClient:
    """Get a configured agent client instance.

    Returns:
        Synchronous agent client configured from environment.
    """
    return SyncAgentClient(AgentConfig.from_env())
