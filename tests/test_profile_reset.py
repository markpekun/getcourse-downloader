import asyncio

import pytest

from getcourse_downloader.domain.errors import DownloadConfigurationError, ExternalServiceError
from getcourse_downloader.infrastructure.browser.profile_lease import _ProfileLease
from getcourse_downloader.infrastructure.platform.paths import AppPaths


def _resetter(paths):
    from getcourse_downloader.infrastructure.browser.profile_reset import BrowserProfileResetter

    return BrowserProfileResetter(paths)


def _paths(tmp_path):
    paths = AppPaths(tmp_path / "data", tmp_path / "browser-profile", tmp_path / "resources")
    paths.ensure_runtime_directories()
    return paths


def test_reset_removes_synthetic_profile_data_and_preserves_app_data_and_videos(tmp_path):
    paths = _paths(tmp_path)
    (paths.session / "cookies.sqlite").write_bytes(b"synthetic placeholder")
    storage = paths.session / "storage" / "default" / "synthetic-origin"
    storage.mkdir(parents=True)
    (storage / "placeholder").write_bytes(b"synthetic storage")
    paths.courses_file.write_bytes(b"course placeholder")
    paths.settings_file.write_bytes(b"settings placeholder")
    video = tmp_path / "videos" / "Lesson_480.mp4"
    video.parent.mkdir()
    video.write_bytes(b"video placeholder")

    asyncio.run(_resetter(paths).clear())

    assert not (paths.session / "cookies.sqlite").exists()
    assert not (paths.session / "storage").exists()
    assert paths.courses_file.read_bytes() == b"course placeholder"
    assert paths.settings_file.read_bytes() == b"settings placeholder"
    assert video.read_bytes() == b"video placeholder"
    lease = _ProfileLease(paths.session)
    lease.acquire()
    lease.release()


def test_reset_refuses_profile_owned_by_another_browser_without_deleting_anything(tmp_path):
    paths = _paths(tmp_path)
    marker = paths.session / "synthetic-marker"
    marker.write_bytes(b"keep")
    owner = _ProfileLease(paths.session)
    owner.acquire()
    try:
        with pytest.raises(ExternalServiceError) as caught:
            asyncio.run(_resetter(paths).clear())
        assert caught.value.code == "BROWSER_PROFILE_BUSY"
        assert marker.read_bytes() == b"keep"
    finally:
        owner.release()


def test_reset_is_repeatable(tmp_path):
    paths = _paths(tmp_path)
    resetter = _resetter(paths)
    asyncio.run(resetter.clear())
    asyncio.run(resetter.clear())
    assert paths.session.is_dir()


def test_reset_rejects_an_app_data_directory_as_profile(tmp_path):
    paths = AppPaths(tmp_path / "data", tmp_path / "data", tmp_path / "resources")
    paths.ensure_runtime_directories()
    paths.courses_file.write_bytes(b"keep")
    with pytest.raises(DownloadConfigurationError):
        asyncio.run(_resetter(paths).clear())
    assert paths.courses_file.read_bytes() == b"keep"


def test_reset_does_not_follow_links_outside_profile(tmp_path):
    paths = _paths(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep"
    marker.write_bytes(b"keep outside")
    link = paths.session / "external-link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks unavailable in this environment")

    asyncio.run(_resetter(paths).clear())

    assert not link.is_symlink()
    assert marker.read_bytes() == b"keep outside"
