"""Photo storage and VLM analysis for family moments."""

import base64
import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger("omni.photos")


@dataclass
class Photo:
    """Photo metadata and analysis result."""
    id: str
    user_scope: str
    file_path: str | None
    file_data: bytes | None
    caption: str | None
    participants: list[str]
    created_at: datetime
    analyzed_at: datetime | None = None

    def to_dict(self):
        """Convert to JSON-serializable dict."""
        return {
            "id": self.id,
            "caption": self.caption,
            "participants": self.participants,
            "createdAt": self.created_at.isoformat(),
            "analyzedAt": self.analyzed_at.isoformat() if self.analyzed_at else None,
        }


class PhotoStore:
    """In-memory photo storage (MVP version). TODO: Move to database."""

    def __init__(self):
        self.photos: dict[str, Photo] = {}

    def add(
        self,
        user_scope: str,
        file_data: bytes,
        caption: str | None = None,
        participants: list[str] | None = None,
    ) -> Photo:
        """Store a photo and return metadata."""
        photo_id = "photo_" + uuid.uuid4().hex[:12]
        photo = Photo(
            id=photo_id,
            user_scope=user_scope,
            file_path=None,
            file_data=file_data,
            caption=caption,
            participants=participants or [],
            created_at=datetime.now(),
        )
        self.photos[photo_id] = photo
        log.info("photo stored: %s (size=%d bytes)", photo_id, len(file_data))
        return photo

    def get(self, photo_id: str) -> Photo | None:
        """Retrieve a photo by ID."""
        return self.photos.get(photo_id)

    def list_for_scope(self, user_scope: str) -> list[Photo]:
        """List all photos for a given user scope."""
        return [
            p for p in self.photos.values() if p.user_scope == user_scope
        ]

    def update_caption(
        self, photo_id: str, caption: str, participants: list[str] | None = None
    ):
        """Update photo metadata after VLM analysis."""
        if photo_id not in self.photos:
            return
        photo = self.photos[photo_id]
        photo.caption = caption
        if participants:
            photo.participants = participants
        photo.analyzed_at = datetime.now()
        log.info("photo updated: %s (caption=%s)", photo_id, caption[:50])


async def analyze_photo_with_vlm(
    photo_data: bytes, text_model, workspace_id: str | None = None
) -> tuple[str, list[str]]:
    """
    Analyze a photo using Qwen VLM to extract caption and participants.

    Returns: (caption, participants_list)
    """
    try:
        # Encode image to base64
        image_base64 = base64.b64encode(photo_data).decode("utf-8")

        # TODO: Implement real qwen-vision API call
        # For MVP, return mock data to move fast
        caption = "看起来是一家人在一起"
        participants = ["爸爸", "妈妈", "女儿"]

        log.info("photo analyzed: caption=%s", caption[:50])
        return caption, participants

    except Exception as e:
        log.error("photo analysis failed: %s", e)
        # Fallback to empty caption if analysis fails
        return "", []
