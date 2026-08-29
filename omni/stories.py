"""Story generation and aggregation for family moments."""

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

log = logging.getLogger("omni.stories")


class StoryType(str, Enum):
    """Types of stories that can be generated."""
    VOICE_SESSION = "voice_session"  # From a voice conversation
    PHOTO_MOMENT = "photo_moment"    # From a photo upload
    MILESTONE = "milestone"           # Birthday, graduation, etc.


@dataclass
class Story:
    """Aggregated story combining memories, photos, and context."""
    id: str
    title: str
    story_type: StoryType
    description: str
    participants: list[str]
    created_at: datetime
    location: str | None = None
    photos_ids: list[str] | None = None
    memory_entry_ids: list[str] | None = None
    confidence: float = 0.8  # 0-1 scale

    def to_dict(self):
        """Convert to JSON-serializable dict."""
        return {
            "id": self.id,
            "title": self.title,
            "type": self.story_type.value,
            "description": self.description,
            "participants": self.participants,
            "createdAt": self.created_at.isoformat(),
            "location": self.location,
            "photoIds": self.photos_ids or [],
            "memoryEntryIds": self.memory_entry_ids or [],
            "confidence": self.confidence,
        }


class StoryStore:
    """In-memory story storage (MVP version). TODO: Move to database."""

    def __init__(self):
        self.stories: dict[str, Story] = {}

    def add(self, story: Story) -> Story:
        """Store a story and return it."""
        self.stories[story.id] = story
        log.info("story stored: %s (%s)", story.id, story.story_type.value)
        return story

    def get(self, story_id: str) -> Story | None:
        """Retrieve a story by ID."""
        return self.stories.get(story_id)

    def list_all(self) -> list[Story]:
        """List all stories, sorted by creation time (newest first)."""
        return sorted(
            self.stories.values(),
            key=lambda s: s.created_at,
            reverse=True
        )


class StoryGenerator:
    """Generate stories by aggregating memories and photos."""

    @staticmethod
    def story_from_memory_entries(
        entry_ids: list[str],
        memory_store,
        title: str | None = None,
        description: str | None = None,
    ) -> Story:
        """Create a story from a list of related memory entries."""
        from uuid import uuid4
        entries = [memory_store.get(eid) for eid in entry_ids if memory_store.get(eid)]

        if not entries:
            return None

        # Extract participants from profile memories
        participants = set()
        for entry in entries:
            if entry.layer == "profile" and ("名字" in entry.text or "是" in entry.text):
                # Simple heuristic: profile entries often mention names
                participants.add(entry.text[:20])

        # Use provided title or generate from first memory
        if not title:
            title = entries[0].text[:50]

        return Story(
            id="story_" + uuid4().hex[:12],
            title=title,
            story_type=StoryType.VOICE_SESSION,
            description=description or "来自对话的记忆",
            participants=list(participants) or ["用户"],
            created_at=entries[0].created_at,
            memory_entry_ids=entry_ids,
            confidence=0.7,
        )

    @staticmethod
    def story_from_photos(
        photo_ids: list,
        photo_store,
        title: str | None = None,
    ) -> Story:
        """Create a story from uploaded photos."""
        from uuid import uuid4
        photos = [photo_store.get(pid) for pid in photo_ids if photo_store.get(pid)]

        if not photos:
            return None

        # Extract participants from photo captions
        participants = set()
        for photo in photos:
            if photo.participants:
                participants.update(photo.participants)

        if not title:
            title = photos[0].caption[:50] if photos[0].caption else "照片时刻"

        return Story(
            id="story_" + uuid4().hex[:12],
            title=title,
            story_type=StoryType.PHOTO_MOMENT,
            description="",  # Will be filled by AI if needed
            participants=list(participants) or ["家人"],
            created_at=photos[0].created_at,
            photos_ids=photo_ids,
            confidence=0.8,
        )
