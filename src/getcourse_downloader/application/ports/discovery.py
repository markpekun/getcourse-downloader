from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from getcourse_downloader.domain.models import Course

AuthRequiredCallback = Callable[[str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class CourseDiscoveryUpdate:
    url: str
    title: str
    lesson_count: int | None = None
    skipped: bool = False

    @property
    def loaded(self) -> bool:
        return self.skipped or self.lesson_count is not None


CourseDiscoveredCallback = Callable[[CourseDiscoveryUpdate], Awaitable[None]]


class CourseDiscoverer(Protocol):
    async def discover(
        self,
        url: str,
        *,
        on_auth_required: AuthRequiredCallback | None = None,
        on_course_discovered: CourseDiscoveredCallback | None = None,
    ) -> Sequence[Course]: ...
