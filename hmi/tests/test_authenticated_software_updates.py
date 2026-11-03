"""Authenticated platform-only updates without client keys; no real network/devices."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import http.cookiejar
import io
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from factory_hmi import updates
from factory_hmi.gateway.software_updates import SoftwareUpdates
from factory_hmi.gateway.platform_software_updates import check_platform_update, stage_platform_update
from factory_hmi.sync.reliability_platform import ReliabilityPlatformClient, PlatformError
from test_software_updater import releases, HOST


def signed_in_client(url="https://platform.invalid"):
    client = ReliabilityPlatformClient(url)
    cookie = http.cookiejar.Cookie(0, "rp1_session", "synthetic-test-session", None, False,
        "platform.invalid", False, False, "/", True, True, None, True, None, None, {}, False)
    client.cookies.set_cookie(cookie)
    return client


def setup_updater(release, package, tmp_path, monkeypatch):
    client = signed_in_client()
    client.software_latest = Mock(return_value={"available": True, "can_update": True, "release": release})
    client.software_package = Mock(side_effect=lambda _id: io.BytesIO(package.read_bytes()))
    platform = SimpleNamespace(_lock=threading.RLock(), _identity="operator", _mode="reliability_v1", _uploader=SimpleNamespace(client=client))
    updater = SoftwareUpdates(platform, tmp_path / "data"); updater.running_version = HOST.current_version
    monkeypatch.setattr(updates, "detect_host", lambda: HOST)
    monkeypatch.delenv("RP1_HMI_UPDATE_PUBLIC_KEY", raising=False)
    return updater, client, platform


def test_online_check_and_download_need_no_per_user_public_key(releases, tmp_path, monkeypatch):
    make, _, _ = releases; release, package = make()
    updater, client, _ = setup_updater(release, package, tmp_path, monkeypatch)
    # An accidental future local signature dependency makes the online test fail.
    monkeypatch.setattr(updates, "verify_release", Mock(side_effect=AssertionError("Online path asked for a local update key")))
    assert updater.check()["available"]
    receipt = updater.download(release["manifest"]["release_id"])
    assert receipt["status"] == "staged"
    assert receipt["release_authentication"] == "authenticated_https_platform"
    assert Path(receipt["package_path"]).read_bytes() == package.read_bytes()
    assert not (tmp_path / "config/update-public-key.pem").exists()
    client.software_package.assert_called_once_with(release["manifest"]["release_id"])


@pytest.mark.parametrize("kind", ["http", "not-logged-in", "no-cookie", "embedded-user", "query", "non-root", "arbitrary-client"])
def test_non_authenticated_or_non_https_sources_are_rejected_before_network(kind):
    client = signed_in_client()
    identity = "operator"
    if kind == "http":client.base_url = "http://platform.invalid"
    elif kind == "not-logged-in":identity = ""
    elif kind == "no-cookie":client.cookies.clear()
    elif kind == "embedded-user":client.base_url = "https://user@platform.invalid"
    elif kind == "query":client.base_url += "?source=elsewhere"
    elif kind == "non-root":client.base_url += "/untrusted"
    elif kind == "arbitrary-client":client = Mock(base_url="https://platform.invalid")
    client.software_latest = Mock()
    with pytest.raises(ValueError):check_platform_update(client, identity=identity, host=asdict(HOST))
    client.software_latest.assert_not_called()


def test_server_expired_session_failure_cannot_be_treated_as_authenticated():
    client = signed_in_client();client.software_latest = Mock(side_effect=PlatformError("expired", status=401))
    with pytest.raises(PlatformError):check_platform_update(client, identity="operator", host=asdict(HOST))


@pytest.mark.parametrize("field,value", [("os_version","22.04"),("architecture","arm64"),("version",HOST.current_version),("sha256","not-a-sha"),("size_bytes",0)])
def test_platform_manifest_target_version_and_digest_contract_remain_checked(releases, field, value):
    make, _, _ = releases; release, _ = make();bad=deepcopy(release);bad["manifest"][field]=value
    client=signed_in_client();client.software_latest=Mock(return_value={"available":True,"can_update":True,"release":bad})
    with pytest.raises(ValueError):check_platform_update(client, identity="operator", host=asdict(HOST))


@pytest.mark.parametrize("failure", ["too-short","too-long","hash","metadata","transport"])
def test_payload_failures_preserve_old_cache_and_remove_partial(releases,tmp_path,monkeypatch,failure):
    make, _, _ = releases; release, package = make();updater,client,_=setup_updater(release,package,tmp_path,monkeypatch)
    first=updater.download(release["manifest"]["release_id"]);pointer=updater.cache_root/"current.json";before=pointer.read_bytes()
    second, next_package=make("2.0.14")
    raw=next_package.read_bytes()
    if failure=="too-short":raw=raw[:-1]
    elif failure=="too-long":raw+=b"extra"
    elif failure=="hash":raw=bytes([raw[0]^1])+raw[1:]
    elif failure=="metadata":second["manifest"]["package_name"]="rp1-test-hmi";second["manifest"]["version"]="2.0.15"
    client.software_latest.return_value={"available":True,"can_update":True,"release":second}
    if failure=="transport":client.software_package.side_effect=OSError("synthetic network failure")
    else:client.software_package.side_effect=lambda _id:io.BytesIO(raw)
    with pytest.raises((ValueError,OSError,RuntimeError)):updater.download(second["manifest"]["release_id"])
    assert pointer.read_bytes()==before
    assert Path(first["package_path"]).exists()
    assert not list((updater.cache_root/"releases").glob(".partial-*"))


def test_account_switch_before_cache_publication_is_rejected(releases,tmp_path,monkeypatch):
    make, _, _=releases;release,package=make();updater,client,platform=setup_updater(release,package,tmp_path,monkeypatch)
    class Changed(io.BytesIO):
        def read(self,*args):
            platform._identity="other-operator"
            return super().read(*args)
    client.software_package.side_effect=lambda _id:Changed(package.read_bytes())
    with pytest.raises(ValueError,match="账号已变化"):updater.download(release["manifest"]["release_id"])
    assert not (updater.cache_root/"current.json").exists()
    assert not list((updater.cache_root/"releases").glob(".partial-*"))


def test_viewer_permission_and_changed_latest_release_still_block_download(releases,tmp_path):
    make,_,_=releases;release,_=make();client=signed_in_client();client.software_package=Mock()
    client.software_latest=Mock(return_value={"available":True,"can_update":False,"release":release})
    with pytest.raises(ValueError,match="无权"):stage_platform_update(client,identity="viewer",release_id=release["manifest"]["release_id"],cache_dir=tmp_path/'cache',host=asdict(HOST))
    client.software_latest.return_value["can_update"]=True
    with pytest.raises(ValueError,match="版本已变化"):stage_platform_update(client,identity="operator",release_id="a"*32,cache_dir=tmp_path/'cache',host=asdict(HOST))
    client.software_package.assert_not_called()


def test_online_cache_does_not_disable_offline_signature_verification(releases,tmp_path,monkeypatch):
    make, _, public=releases;release,package=make();updater,_,_=setup_updater(release,package,tmp_path,monkeypatch)
    receipt=updater.download(release['manifest']['release_id'])
    bad=deepcopy(release);bad['manifest']['notes']+='tampered'
    with pytest.raises(ValueError,match='签名'):updates.stage_release(bad,public_key_path=public,host=HOST,cache_dir=updater.cache_root,package_stream=io.BytesIO(package.read_bytes()))
    assert Path(receipt['package_path']).read_bytes()==package.read_bytes()
