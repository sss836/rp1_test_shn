"""Real temporary Ed25519/DEB checks; never install or stop a device process."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError

import pytest

from factory_hmi import updates

HOST = updates.HostInfo("ubuntu", "24.04", "amd64", "2.0.12")


def command(argv):
    result = subprocess.run(argv, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr.decode()
    return result.stdout


@pytest.fixture
def releases(tmp_path):
    private, public = tmp_path / "test-only-private.pem", tmp_path / "test-only-public.pem"
    command(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(private)])
    command(["openssl", "pkey", "-in", str(private), "-pubout", "-out", str(public)])
    public.chmod(0o644)
    counter = 0
    def make(version="2.0.13", *, package_name="rp1-test-hmi", architecture="amd64"):
        nonlocal counter
        counter += 1
        root = tmp_path / f"deb-input-{counter}"
        (root / "DEBIAN").mkdir(parents=True)
        (root / "DEBIAN/control").write_text(
            f"Package: {package_name}\nVersion: {version}\nArchitecture: {architecture}\n"
            "Maintainer: Isolated Test <test@example.invalid>\nDescription: Test-only inert DEB\n")
        (root / "test-only.txt").write_text(f"inert payload {counter}")
        package = tmp_path / f"rp1-test-{counter}.deb"
        command(["dpkg-deb", "--build", str(root), str(package)])
        release = updates.prepare_release(package, private_key_path=private,
                                          os_id="ubuntu", os_version="24.04",
                                          output=tmp_path / f"release-{counter}.json",
                                          notes="隔离签名测试")
        return release, package
    def sign(release):
        message = tmp_path / "sign-message"
        signature = tmp_path / "sign-signature"
        message.write_bytes(updates.canonical_manifest(release["manifest"]))
        command(["openssl", "pkeyutl", "-sign", "-inkey", str(private), "-rawin",
                 "-in", str(message), "-out", str(signature)])
        release["signature"] = base64.b64encode(signature.read_bytes()).decode()
        return release
    return make, sign, public


def test_real_signature_and_canonical_chinese_manifest(releases):
    make, _, public = releases
    release, _ = make()
    assert updates.verify_release(release, public, HOST) == release["manifest"]
    assert "隔离签名测试".encode() in updates.canonical_manifest(release["manifest"])


@pytest.mark.parametrize("mutation", ["signature", "notes", "unknown-field", "missing-field", "bool-size", "path", "zero-size"])
def test_tampering_and_malformed_manifest_rejected(releases, mutation):
    make, _, public = releases
    release, _ = make()
    if mutation == "signature":
        release["signature"] = base64.b64encode(b"\x00" * 64).decode()
    elif mutation == "notes":
        release["manifest"]["notes"] += "tampered"
    elif mutation == "unknown-field":
        release["manifest"]["token"] = "must-not-cache"
    elif mutation == "missing-field":
        del release["manifest"]["architecture"]
    elif mutation == "bool-size":
        release["manifest"]["size_bytes"] = True
    elif mutation == "path":
        release["manifest"]["filename"] = "../escape.deb"
    else:
        release["manifest"]["size_bytes"] = 0
    with pytest.raises(ValueError):
        updates.verify_release(release, public, HOST)


@pytest.mark.parametrize("host", [
    updates.HostInfo("debian", "12", "amd64", "2.0.12"),
    updates.HostInfo("ubuntu", "26.04", "amd64", "2.0.12"),
    updates.HostInfo("ubuntu", "22.04", "amd64", "2.0.12"),
    updates.HostInfo("ubuntu", "24.04", "arm64", "2.0.12"),
    updates.HostInfo("ubuntu", "24.04", "amd64", "2.0.13"),
    updates.HostInfo("ubuntu", "24.04", "amd64", "2.0.14"),
    updates.HostInfo("ubuntu", "24.04", "amd64", ""),
])
def test_unknown_host_arch_mismatch_and_downgrade_fail_closed(releases, host):
    make, _, public = releases
    release, _ = make()
    with pytest.raises(ValueError):
        updates.verify_release(release, public, host)


def test_key_missing_or_writable_or_wrong_key_rejected(releases, tmp_path):
    make, _, public = releases
    release, _ = make()
    with pytest.raises(ValueError, match="公钥"):
        updates.verify_release(release, tmp_path / "missing", HOST)
    public.chmod(0o666)
    with pytest.raises(ValueError, match="公钥"):
        updates.verify_release(release, public, HOST)
    public.chmod(0o644)
    private2, public2 = tmp_path / "other-private", tmp_path / "other-public"
    command(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(private2)])
    command(["openssl", "pkey", "-in", str(private2), "-pubout", "-out", str(public2)])
    public2.chmod(0o644)
    with pytest.raises(ValueError, match="签名"):
        updates.verify_release(release, public2, HOST)


def test_stage_atomic_cache_idempotency_and_previous_package_preservation(releases, tmp_path):
    make, _, public = releases
    cache = tmp_path / "private-cache"
    release1, package1 = make()
    receipt1 = updates.stage_release(release1, public_key_path=public, host=HOST,
                                     cache_dir=cache, package_stream=io.BytesIO(package1.read_bytes()))
    assert receipt1["status"] == "staged"
    assert Path(receipt1["package_path"]).read_bytes() == package1.read_bytes()
    assert receipt1["previous_package_path"] is None
    # An existing release is re-verified, without reading the caller's stream.
    receipt2 = updates.stage_release(release1, public_key_path=public, host=HOST,
                                     cache_dir=cache, package_stream=io.BytesIO(b"unused"))
    assert receipt2 == receipt1
    release3, package3 = make("2.0.14")
    receipt3 = updates.stage_release(release3, public_key_path=public, host=HOST,
                                     cache_dir=cache, package_stream=io.BytesIO(package3.read_bytes()))
    assert receipt3["previous_package_path"] == receipt1["package_path"]
    assert Path(receipt1["package_path"]).exists()
    assert not list((cache / "releases").glob(".partial-*"))
    assert cache.stat().st_mode & 0o777 == 0o700
    assert "token" not in "".join(p.read_text() for p in cache.rglob("*.json"))


@pytest.mark.parametrize("failure", ["too-long", "too-short", "hash", "metadata", "transfer"])
def test_failed_stage_retains_prior_cache_and_cleans_partial(releases, tmp_path, failure):
    make, sign, public = releases
    cache = tmp_path / "cache"
    first, first_package = make()
    receipt = updates.stage_release(first, public_key_path=public, cache_dir=cache, host=HOST,
                                    package_stream=io.BytesIO(first_package.read_bytes()))
    before = (cache / "current.json").read_bytes()
    release, package = make("2.0.14")
    content = package.read_bytes()
    if failure == "too-long":
        content += b"extra"
    elif failure == "too-short":
        content = content[:-1]
    elif failure == "hash":
        content = b"X" + content[1:]
    elif failure == "metadata":
        release["manifest"]["version"] = "2.0.15"
        sign(release)
    stream = io.BytesIO(content)
    if failure == "transfer":
        class Broken:
            def read(self, _count):
                raise OSError("isolated interrupted stream")
        stream = Broken()
    with pytest.raises((ValueError, RuntimeError)):
        updates.stage_release(release, public_key_path=public, cache_dir=cache, host=HOST, package_stream=stream)
    assert (cache / "current.json").read_bytes() == before
    assert Path(receipt["package_path"]).exists()
    assert not list((cache / "releases").glob(".partial-*"))
    assert not (cache / "releases" / release["manifest"]["release_id"]).exists()


def test_cache_permissions_and_symlinks_rejected(releases, tmp_path):
    make, _, public = releases
    release, package = make()
    open_cache = tmp_path / "open-cache"
    open_cache.mkdir(mode=0o755)
    for cache in (open_cache, tmp_path / "cache-link"):
        if cache.name == "cache-link":
            cache.symlink_to(open_cache)
        with pytest.raises(ValueError, match="缓存"):
            updates.stage_release(release, public_key_path=public, host=HOST, cache_dir=cache,
                                  package_stream=io.BytesIO(package.read_bytes()))


def test_debian_version_ordering_and_release_id_collision(releases, tmp_path):
    make, sign, public = releases
    release, package = make("2.0.13~rc1")
    assert updates.verify_release(release, public, HOST)
    with pytest.raises(ValueError):
        updates.verify_release(release, public, updates.HostInfo("ubuntu", "24.04", "amd64", "2.0.13"))
    cache = tmp_path / "cache"
    updates.stage_release(release, public_key_path=public, host=HOST, cache_dir=cache,
                          package_stream=io.BytesIO(package.read_bytes()))
    release["manifest"]["notes"] = "different signed description"
    sign(release)
    with pytest.raises(ValueError, match="禁止覆盖"):
        updates.stage_release(release, public_key_path=public, host=HOST, cache_dir=cache,
                              package_stream=io.BytesIO(package.read_bytes()))


def test_redirects_and_credentials_are_never_forwarded_or_logged(monkeypatch):
    assert updates._NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid") is None
    class Opener:
        def open(self, request, timeout):
            raise HTTPError("https://secret.invalid/?token=do-not-log", 302, "secret", {}, None)
    monkeypatch.setattr(updates, "build_opener", lambda *args: Opener())
    with pytest.raises(RuntimeError) as caught:
        with updates._download("https://source.invalid/package", {"Authorization": "secret"}):
            pass
    assert "secret" not in str(caught.value)
    for address in ("file:///tmp/pkg", "ftp://source/package", "https://user:pass@source/package"):
        with pytest.raises(ValueError):
            updates._url(address)


def test_install_preflight_and_explicit_install_never_install_or_stop(releases, tmp_path, monkeypatch):
    make, _, public = releases
    release, package = make()
    receipt = updates.stage_release(release, public_key_path=public, host=HOST, cache_dir=tmp_path / "cache",
                                    package_stream=io.BytesIO(package.read_bytes()))
    state = tmp_path / "state"
    (state / "run").mkdir(parents=True)
    real_run = updates._run
    calls = []
    def read_only_run(argv, **kwargs):
        calls.append(argv)
        if "status" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"running": False, "state_root": str(state)}), "")
        if argv[0] == "apt-get":
            assert argv[1:3] == ["--simulate", "--no-download"]
            return subprocess.CompletedProcess(argv, 0, "simulated only", "")
        return real_run(argv, **kwargs)
    monkeypatch.setattr(updates, "_run", read_only_run)
    monkeypatch.setattr(updates, "installed_version", lambda: "2.0.12")
    with (state / "run/launcher.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = updates.install_preflight(receipt["release_path"], public_key_path=public, state_dir=state, host=HOST)
        assert result["ready"] is False
        assert any("启动锁" in text for text in result["reasons"])
    with pytest.raises(RuntimeError, match="真实电机失能"):
        updates.protected_install(receipt["release_path"], public_key_path=public, state_dir=state, host=HOST)
    assert all("stop" not in argv and "-i" not in argv and "--install" not in argv for argv in calls)


def test_post_install_verification_rejects_old_process_and_false_metadata(releases, tmp_path, monkeypatch):
    make, _, public = releases
    release, _ = make()
    path = tmp_path / "post-release.json"
    path.write_text(json.dumps(release))
    state = tmp_path / "state"
    state.mkdir()
    pid = os.getpid()
    raw = Path(f"/proc/{pid}/stat").read_text()
    ticks = raw[raw.rfind(")") + 2:].split()[19]
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    real_run = updates._run
    def run(argv, **kwargs):
        if "status" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"running": True, "state_root": str(state),
                "gateway_pid": pid, "gateway": "http://127.0.0.1:8766"}), "")
        return real_run(argv, **kwargs)
    monkeypatch.setattr(updates, "_run", run)
    monkeypatch.setattr(updates, "installed_version", lambda: "2.0.13")
    health = {"ok": True, "software": {"running_version": "2.0.13", "pid": pid, "start_ticks": ticks}}
    @contextmanager
    def download(*args):
        yield io.BytesIO(json.dumps(health).encode())
    monkeypatch.setattr(updates, "_download", download)
    same = {"pid": pid, "start_ticks": ticks, "boot_id": boot}
    with pytest.raises(ValueError, match="旧进程"):
        updates.verify_installation(path, public_key_path=public, state_dir=state, previous_identity=same, host=HOST)
    prior = {**same, "start_ticks": "1"}
    assert updates.verify_installation(path, public_key_path=public, state_dir=state, previous_identity=prior, host=HOST)["verified"]
    health["software"]["running_version"] = "2.0.12"
    with pytest.raises(ValueError, match="尚未全部一致"):
        updates.verify_installation(path, public_key_path=public, state_dir=state, previous_identity=prior, host=HOST)


def test_cli_help_and_status_have_no_motion_or_numpy_dependency(tmp_path):
    launcher = Path(__file__).resolve().parents[1] / "launch.py"
    for argv in (["update", "--help"], ["check-update", "--help"],
                 ["status", "--state-dir", str(tmp_path / "state")]):
        result = subprocess.run([sys.executable, "-I", "-S", str(launcher), *argv],
                                capture_output=True, text=True, timeout=5)
        assert result.returncode == 0, result.stderr
    result = subprocess.run([sys.executable, "-I", "-S", str(launcher), "update", "install"],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 2
    assert "--release" in result.stderr


@pytest.mark.parametrize("layout", ["checkout", "deb"])
def test_launcher_lookup_uses_actual_checkout_and_deb_layout(tmp_path, monkeypatch, layout):
    root = tmp_path / layout
    module = root / "hmi/factory_hmi/updates.py"
    module.parent.mkdir(parents=True)
    module.write_text("# inert updater layout fixture\n")
    launcher = root / ("hmi/launch.py" if layout == "checkout" else "launch.py")
    launcher.write_text("# inert launcher: lookup must not execute this file\n")
    monkeypatch.setattr(updates, "__file__", str(module))
    assert updates._launcher_command() == [sys.executable, str(launcher)]


def test_launcher_lookup_does_not_fall_back_to_path_or_symlink(tmp_path, monkeypatch):
    module = tmp_path / "app/hmi/factory_hmi/updates.py"
    module.parent.mkdir(parents=True)
    module.write_text("# inert updater\n")
    unrelated = tmp_path / "unrelated-launch.py"
    unrelated.write_text("# must not be selected\n")
    (module.parents[1] / "launch.py").symlink_to(unrelated)
    monkeypatch.setattr(updates, "__file__", str(module))
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(RuntimeError, match="启动器"):
        updates._launcher_command()
