import json
from pathlib import Path

from getcourse_downloader.domain.models import MediaKind
from getcourse_downloader.infrastructure.storage.download_catalog import (
    DownloadedMedia,
    JsonDownloadCatalog,
)


def test_download_catalog_returns_quality_only_for_existing_nonempty_files(tmp_path):
    catalog = JsonDownloadCatalog(tmp_path / "data" / "downloads.json")
    stem = tmp_path / "downloads" / "Курс" / "Урок"
    media_path = stem.parent / "Урок.mp4"
    media_path.parent.mkdir(parents=True)
    media_path.write_bytes(b"video")

    catalog.save(
        "https://school/lesson/1",
        stem,
        (DownloadedMedia(media_path, "1080p"),),
    )

    assert catalog.find("https://school/lesson/1", stem) == (DownloadedMedia(media_path, "1080p"),)
    media_path.unlink()
    assert catalog.find("https://school/lesson/1", stem) == ()


def test_download_catalog_is_scoped_to_lesson_and_output_location(tmp_path):
    catalog = JsonDownloadCatalog(tmp_path / "downloads.json")
    stem = tmp_path / "target" / "Lesson"
    media_path = Path(f"{stem}.mp4")
    media_path.parent.mkdir(parents=True)
    media_path.write_bytes(b"video")
    catalog.save("https://school/lesson/1", stem, (DownloadedMedia(media_path, "720p"),))

    assert catalog.find("https://school/lesson/2", stem) == ()
    assert catalog.find("https://school/lesson/1", tmp_path / "other" / "Lesson") == ()


def test_invalid_utf8_catalog_does_not_prevent_downloading_again(tmp_path):
    path = tmp_path / "downloads.json"
    path.write_bytes(b"\xff\xfe\x00")

    assert JsonDownloadCatalog(path).find("https://school/lesson/1", tmp_path / "Lesson") == ()


def test_catalog_detects_same_stem_owned_by_another_lesson(tmp_path):
    catalog = JsonDownloadCatalog(tmp_path / "downloads.json")
    stem = tmp_path / "target" / "Lesson"
    media_path = stem.parent / "Lesson_720.mp4"
    media_path.parent.mkdir(parents=True)
    media_path.write_bytes(b"video")
    catalog.save("https://school/lesson/1", stem, (DownloadedMedia(media_path, "720p"),))

    assert not catalog.has_stem_conflict("https://school/lesson/1", stem)
    assert catalog.has_stem_conflict("https://school/lesson/2", stem)


def test_catalog_keeps_audio_and_video_records_for_the_same_lesson_separate(tmp_path):
    catalog = JsonDownloadCatalog(tmp_path / "downloads.json")
    stem = tmp_path / "target" / "Lesson"
    video_path = stem.with_suffix(".mp4")
    audio_path = stem.with_suffix(".mp3")
    video_path.parent.mkdir(parents=True)
    video_path.write_bytes(b"video")
    audio_path.write_bytes(b"audio")

    catalog.save(
        "https://school/lesson/1",
        stem,
        (DownloadedMedia(video_path, "720p", MediaKind.VIDEO),),
    )
    catalog.save(
        "https://school/lesson/1",
        stem,
        (DownloadedMedia(audio_path, kind=MediaKind.AUDIO),),
    )

    assert catalog.find("https://school/lesson/1", stem, MediaKind.VIDEO) == (
        DownloadedMedia(video_path, "720p", MediaKind.VIDEO),
    )
    assert catalog.find("https://school/lesson/1", stem, MediaKind.AUDIO) == (
        DownloadedMedia(audio_path, "", MediaKind.AUDIO),
    )


def test_schema_one_catalog_entries_are_read_as_video(tmp_path):
    stem = tmp_path / "target" / "Lesson"
    media_path = stem.with_suffix(".mp4")
    media_path.parent.mkdir(parents=True)
    media_path.write_bytes(b"video")
    catalog_path = tmp_path / "downloads.json"
    catalog_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "records": {
                    f"https://school/lesson/1\x1f{stem.resolve()}": {
                        "lesson_url": "https://school/lesson/1",
                        "output_stem": str(stem.resolve()),
                        "media": [{"path": str(media_path.resolve()), "quality": "720p"}],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert JsonDownloadCatalog(catalog_path).find(
        "https://school/lesson/1", stem, MediaKind.VIDEO
    ) == (DownloadedMedia(media_path, "720p", MediaKind.VIDEO),)
