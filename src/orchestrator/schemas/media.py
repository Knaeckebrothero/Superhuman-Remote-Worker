"""Media proxy request contracts."""

from pydantic import BaseModel, Field

from orchestrator.services.remote_image import MAX_REMOTE_IMAGE_URL_CHARS


class RemoteImageRequest(BaseModel):
    """An exact external image URL the Cockpit user chose to load once."""

    url: str = Field(min_length=1, max_length=MAX_REMOTE_IMAGE_URL_CHARS)
