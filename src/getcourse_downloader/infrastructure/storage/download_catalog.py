from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DownloadedMedia:
    path: Path
    quality: str = ""


class JsonDownloadCatalog:
    """Small local index used to skip finished lessons without opening Firefox."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @staticmethod
    def _key(lesson_url: str, output_stem: Path) -> str:
        return f"{lesson_url}\x1f{output_stem.resolve()}"

    def _load(self) -> dict[str, object]:
        if not self._path.is_file():
            return {"schema_version": 1, "records": {}}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {"schema_version": 1, "records": {}}
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            return {"schema_version": 1, "records": {}}
        if not isinstance(payload.get("records"), dict):
            payload["records"] = {}
        return payload

    def _save(self, payload: dict[str, object]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            dir=self._path.parent,
        )
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
        finally:
            temporary.unlink(missing_ok=True)

    def find(self, lesson_url: str, output_stem: Path) -> tuple[DownloadedMedia, ...]:
        payload = self._load()
        records = payload["records"]
        assert isinstance(records, dict)
        raw_record = records.get(self._key(lesson_url, output_stem))
        if not isinstance(raw_record, dict) or not isinstance(raw_record.get("media"), list):
            return ()

        media: list[DownloadedMedia] = []
        for raw_media in raw_record["media"]:
            if not isinstance(raw_media, dict) or not isinstance(raw_media.get("path"), str):
                return ()
            path = Path(raw_media["path"])
            try:
                if not path.is_file() or path.stat().st_size <= 0:
                    return ()
            except OSError:
                return ()
            quality = raw_media.get("quality", "")
            media.append(DownloadedMedia(path, quality if isinstance(quality, str) else ""))
        return tuple(media)

    def has_stem_conflict(self, lesson_url: str, output_stem: Path) -> bool:
        payload = self._load()
        records = payload["records"]
        assert isinstance(records, dict)
        expected = str(output_stem.resolve()).casefold()
        for raw_record in records.values():
            if not isinstance(raw_record, dict):
                continue
            recorded_stem = raw_record.get("output_stem")
            recorded_url = raw_record.get("lesson_url")
            if (
                isinstance(recorded_stem, str)
                and isinstance(recorded_url, str)
                and recorded_stem.casefold() == expected
                and recorded_url != lesson_url
            ):
                return True
        return False

    def save(
        self,
        lesson_url: str,
        output_stem: Path,
        media: tuple[DownloadedMedia, ...],
    ) -> None:
        if not media:
            return
        payload = self._load()
        records = payload["records"]
        assert isinstance(records, dict)
        records[self._key(lesson_url, output_stem)] = {
            "lesson_url": lesson_url,
            "output_stem": str(output_stem.resolve()),
            "media": [
                {"path": str(item.path.resolve()), "quality": item.quality} for item in media
            ],
        }
        self._save(payload)
