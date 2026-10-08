from dataclasses import dataclass, field
from enum import StrEnum


class CompletionStep(StrEnum):
    RESULT = "result"
    SUPPORT = "support"
    INSTRUCTIONS = "instructions"


@dataclass(frozen=True, slots=True)
class CompletionResult:
    message: str
    is_error: bool = False
    is_warning: bool = False
    cancelled: bool = False


@dataclass(slots=True)
class CompletionViewState:
    result: CompletionResult
    step: CompletionStep = CompletionStep.RESULT


@dataclass(slots=True)
class CoursesViewState:
    save_path: str
    quality: str = "auto"
    downloading: bool = False
    cancelling: bool = False
    selected_lesson_urls: set[str] = field(default_factory=set)
    expanded_course_urls: set[str] = field(default_factory=set)
    search_query: str = ""
