from pathlib import Path

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
