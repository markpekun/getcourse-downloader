import os

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
