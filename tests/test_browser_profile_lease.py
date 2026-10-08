import os
import subprocess
import sys
import time

import pytest

from getcourse_downloader.domain.errors import ExternalServiceError
from getcourse_downloader.infrastructure.browser.playwright import _ProfileLease


def test_profile_lease_rejects_another_live_owner(tmp_path):
    owner = tmp_path / ".gcd-profile-owner"
    owner.write_text(str(os.getpid()), encoding="ascii")

    with pytest.raises(ExternalServiceError, match="уже используется") as captured:
        _ProfileLease(tmp_path).acquire()

    assert getattr(captured.value, "code", "") == "BROWSER_PROFILE_BUSY"


def test_profile_lease_replaces_stale_owner_and_releases(tmp_path):
    owner = tmp_path / ".gcd-profile-owner"
    owner.write_text("99999999", encoding="ascii")
    lease = _ProfileLease(tmp_path)

    lease.acquire()
    assert owner.read_text(encoding="ascii") == str(os.getpid())
    lease.release()
    assert not owner.exists()


def test_profile_lease_does_not_steal_lock_before_owner_pid_is_written(tmp_path):
    owner = tmp_path / ".gcd-profile-owner"
    owner.write_bytes(b"")
    with pytest.raises(ExternalServiceError):
        _ProfileLease(tmp_path).acquire()
    assert owner.read_bytes() == b""


def test_failed_acquisition_cannot_release_another_lease(tmp_path):
    first = _ProfileLease(tmp_path)
    second = _ProfileLease(tmp_path)
    first.acquire()
    try:
        with pytest.raises(ExternalServiceError):
            second.acquire()
        second.release()
        with pytest.raises(ExternalServiceError):
            _ProfileLease(tmp_path).acquire()
    finally:
        first.release()


def test_profile_lease_stays_exclusive_if_owner_metadata_disappears(tmp_path):
    first = _ProfileLease(tmp_path)
    second = _ProfileLease(tmp_path)
    first.acquire()
    try:
        (tmp_path / ".gcd-profile-owner").unlink()
        with pytest.raises(ExternalServiceError):
            second.acquire()
    finally:
        second.release()
        first.release()


@pytest.mark.skipif(os.name != "nt", reason="Windows PID reuse recovery")
def test_profile_lease_recovers_legacy_owner_from_before_process_started(tmp_path):
    owner = tmp_path / ".gcd-profile-owner"
    owner.write_text(str(os.getpid()), encoding="ascii")
    os.utime(owner, (1, 1))
    lease = _ProfileLease(tmp_path)
    try:
        lease.acquire()
        assert owner.read_text(encoding="ascii") == str(os.getpid())
    finally:
        lease.release()


@pytest.mark.integration
def test_profile_lease_recovers_after_forced_exit_and_preserves_profile(tmp_path):
    marker = tmp_path / "persisted-session-placeholder"
    marker.write_bytes(b"synthetic session data")
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; "
            "from getcourse_downloader.infrastructure.browser.playwright import _ProfileLease; "
            "lease = _ProfileLease(Path(sys.argv[1])); lease.acquire(); "
            "print('ready', flush=True); sys.stdin.read()",
            str(tmp_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    lease = _ProfileLease(tmp_path)
    try:
        assert child.stdout.readline().strip() == "ready"
        with pytest.raises(ExternalServiceError):
            _ProfileLease(tmp_path).acquire()
        child.kill()
        child.wait(timeout=5)
        deadline = time.monotonic() + 3
        while True:
            try:
                lease.acquire()
                break
            except ExternalServiceError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        assert marker.read_bytes() == b"synthetic session data"
    finally:
        lease.release()
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        child.stdin.close()
        child.stdout.close()
        child.stderr.close()
