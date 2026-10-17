from typing import Any, Literal

from pydantic import BaseModel, Field

UPLOAD_SOURCE_KEY = "upload"


class IngestOrigin(BaseModel):
    """Where an incoming file came from and what to attach to it."""

    source_key: str = UPLOAD_SOURCE_KEY
    remote_id: str | None = None
    url: str | None = None
    source_id: int | None = None
    job_run_id: int | None = None
    tags: list[str] = Field(default_factory=list)
    # Tags from add-ons are crawler tags; tags typed on the upload page are user tags.
    tag_origin: Literal["crawler", "user"] = "crawler"
    title: str | None = None
    description: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    force: bool = False
    # A crawl source's default folder: stored in files.metadata of new files so the archive
    # panel preselects it.
    default_folder_id: int | None = None
