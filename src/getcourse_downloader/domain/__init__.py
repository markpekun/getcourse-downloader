"""Pure domain models and rules."""

from getcourse_downloader.domain.events import DownloadEvent, DownloadEventType
from getcourse_downloader.domain.models import (
    Course,
    DownloadRequest,
    DownloadSummary,
    Lesson,
    MediaKind,
    MediaSelection,
    SelectedLesson,
    Settings,
    VideoQuality,
)

__all__ = [
    "Course",
    "DownloadEvent",
    "DownloadEventType",
    "DownloadRequest",
    "DownloadSummary",
    "Lesson",
    "MediaKind",
    "MediaSelection",
    "SelectedLesson",
    "Settings",
    "VideoQuality",
]
