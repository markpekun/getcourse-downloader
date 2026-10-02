from dataclasses import dataclass, field

from getcourse_downloader.domain.models import MediaSelection


@dataclass(slots=True)
class CoursesViewState:
    save_path: str
    media_selection: MediaSelection = field(default_factory=MediaSelection.video_and_audio)
    quality: str = "auto"
    downloading: bool = False
    cancelling: bool = False
    selected_lesson_urls: set[str] = field(default_factory=set)
    expanded_course_urls: set[str] = field(default_factory=set)
    search_query: str = ""
