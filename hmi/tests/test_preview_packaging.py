"""Preview page and package completeness, with no device construction."""
import importlib.util
import shutil
import subprocess
import sys
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from factory_hmi.gateway import preview
from factory_hmi.preview_assets import validate_preview_assets

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("preview_deb_builder", ROOT / "tools/build_local_deb.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def assets(root):
    (root / "assets").mkdir(parents=True)
    (root / "assets/app.js").write_text("document.title = 'preview';")
    (root / "assets/app.css").write_text("body { color: black; }")
    (root / "index.html").write_text('<script type="module" src="/preview/assets/app.js"></script><link rel="stylesheet" href="/preview/assets/app.css">')


def client(tmp_path, monkeypatch, complete=True):
    root = tmp_path / "factory_hmi/resources/local-preview"
    if complete:
        assets(root)
    monkeypatch.setattr(preview, "__file__", str(tmp_path / "factory_hmi/gateway/preview.py"))
    controller = SimpleNamespace(telemetry_hub=SimpleNamespace(run_id="run-1"))
    hub = SimpleNamespace(_lock=threading.Lock(), _key=lambda owner: owner,
                          _sessions={"owner": SimpleNamespace(controller=controller)})
    def require_token(token):
        if token != "operator":
            raise HTTPException(401, "invalid operator")
    app = FastAPI()
    preview.attach_preview(app, hub, require_token, lambda request, client_id: "owner")
    return TestClient(app, base_url="http://127.0.0.1:8766"), root


def test_preview_page_assets_and_single_use_ticket(tmp_path, monkeypatch):
    c, root = client(tmp_path, monkeypatch)
    assert c.post("/api/v1/preview/launch").status_code == 401
    launched = c.post("/api/v1/preview/launch", headers={"x-rp1-operator-token": "operator"})
    assert launched.status_code == 200
    url = launched.json()["url"]
    page = c.get(urlsplit(url).path)
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert page.headers["cache-control"] == "no-store"
    assert c.get("/preview/assets/app.js").status_code == 200
    assert c.get("/preview/assets/app.css").status_code == 200
    ticket = urlsplit(url).fragment.removeprefix("ticket=")
    assert c.post("/api/v1/preview/session", json={"ticket": ticket}).status_code == 200
    assert c.post("/api/v1/preview/session", json={"ticket": ticket}).status_code == 401
    assert c.get("/preview/", headers={"host": "other-host"}).status_code == 403


@pytest.mark.parametrize("broken", ["missing", "index", "js", "css"])
def test_missing_or_partial_resources_do_not_issue_tickets(tmp_path, monkeypatch, broken):
    c, root = client(tmp_path, monkeypatch, complete=broken != "missing")
    if broken != "missing":
        (root / {"index": "index.html", "js": "assets/app.js", "css": "assets/app.css"}[broken]).unlink()
    assert c.get("/preview/").status_code == 503
    response = c.post("/api/v1/preview/launch", headers={"x-rp1-operator-token": "operator"})
    assert response.status_code == 503 and "预览资源" in response.json()["detail"]
    assert not c.app.state.preview.tickets


def test_preview_assets_recover_after_startup_without_resources(tmp_path, monkeypatch):
    c, root = client(tmp_path, monkeypatch, complete=False)
    headers = {"x-rp1-operator-token": "operator"}
    asset_url = "/preview/assets/app.js"
    assert c.get("/preview/").status_code == 503
    assert c.post("/api/v1/preview/launch", headers=headers).status_code == 503
    assert c.get(asset_url).status_code == 404

    assets(root)
    launched = c.post("/api/v1/preview/launch", headers=headers)
    assert launched.status_code == 200
    assert c.get(urlsplit(launched.json()["url"]).path).status_code == 200
    script = c.get(asset_url)
    assert script.status_code == 200 and "javascript" in script.headers["content-type"]
    assert script.content == (root / "assets/app.js").read_bytes()
    assert c.head(asset_url).status_code == 200
    assert c.get(asset_url, headers={"If-None-Match": script.headers["etag"]}).status_code == 304
    assert c.get("/preview/assets/app.css").status_code == 200


def test_preview_assets_obey_local_boundary_and_reject_path_escape(tmp_path, monkeypatch):
    c, root = client(tmp_path, monkeypatch)
    asset_url = "/preview/assets/app.js"
    assert c.get(asset_url, headers={"host": "other-host"}).status_code == 403
    assert c.get(asset_url, headers={"origin": "https://other-host"}).status_code == 403
    (root / "assets/escape.js").symlink_to(root / "index.html")
    assert c.get("/preview/assets/escape.js").status_code == 404
    assert c.get("/preview/assets/%2e%2e/index.html").status_code == 404


def test_ticket_expiry_is_unchanged(tmp_path, monkeypatch):
    c, _ = client(tmp_path, monkeypatch)
    monkeypatch.setattr(preview.time, "monotonic", lambda: 100)
    ticket = c.app.state.preview.issue("owner", "run-1")
    monkeypatch.setattr(preview.time, "monotonic", lambda: 131)
    assert c.post("/api/v1/preview/session", json={"ticket": ticket}).status_code == 401


def test_builder_rejects_missing_preview_before_runtime_or_output(tmp_path, monkeypatch):
    (tmp_path / "factory_hmi").mkdir()
    shutil.copy2(ROOT / "factory_hmi/preview_assets.py", tmp_path / "factory_hmi/preview_assets.py")
    monkeypatch.setattr(builder, "SOURCE", tmp_path)
    monkeypatch.setattr("sys.argv", ["builder", "--runtime-dir", str(tmp_path / "absent-runtime")])
    with pytest.raises(RuntimeError, match="预览资源"):
        builder.main()
    assert not (tmp_path / "dist").exists()


def test_copy_filter_preserves_complete_preview(tmp_path):
    source = tmp_path / "source"
    assets(source)
    target = tmp_path / "stage"
    shutil.copytree(source, target, ignore=builder.ignored)
    assert validate_preview_assets(target) == ["assets/app.js", "assets/app.css"]
    (target / "assets/app.js").unlink()
    with pytest.raises(RuntimeError, match="预览资源"):
        validate_preview_assets(target)


def test_validator_rejects_path_escape(tmp_path):
    assets(tmp_path)
    (tmp_path / "index.html").write_text('<script src="/preview/assets/../secret.js"></script>')
    with pytest.raises(RuntimeError, match="预览资源"):
        validate_preview_assets(tmp_path)


def test_pyinstaller_gateway_validates_and_includes_preview(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("preview_frozen_builder", ROOT / "tools/build_factory_hmi.py")
    frozen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(frozen)
    (tmp_path / "factory_hmi").mkdir()
    shutil.copy2(ROOT / "factory_hmi/preview_assets.py", tmp_path / "factory_hmi/preview_assets.py")
    monkeypatch.setattr(frozen, "ROOT", tmp_path)
    with pytest.raises(RuntimeError, match="预览资源"):
        frozen.build("gateway", one_dir=True, motors_py=tmp_path / "sdk.so")
    assets(tmp_path / "factory_hmi/resources/local-preview")
    monkeypatch.setattr(frozen.importlib.util, "find_spec", lambda name: object())
    (tmp_path / "factory_hmi/gateway").mkdir()
    (tmp_path / "factory_hmi/gateway/__main__.py").write_text("")
    commands = []
    monkeypatch.setattr(frozen.subprocess, "run", lambda command, **kwargs: commands.append(command))
    frozen.build("gateway", one_dir=True, motors_py=tmp_path / "sdk.so")
    assert any("factory_hmi/resources/local-preview" in str(arg) for arg in commands[-1])


def test_validator_works_with_trimmed_html_package_shadowing_stdlib(tmp_path):
    # Reproduce .local-runtime/html containing entities but no parser, without
    # modifying that runtime or relying on the developer machine's install.
    runtime = tmp_path / "runtime"
    (runtime / "html").mkdir(parents=True)
    (runtime / "html/__init__.py").write_text("")
    (runtime / "html/entities.py").write_text("")
    root = tmp_path / "preview"
    assets(root)
    code = "import runpy, sys; validate = runpy.run_path(sys.argv[1])['validate_preview_assets']; print(validate(__import__('pathlib').Path(sys.argv[2])))"
    result = subprocess.run([sys.executable, "-S", "-c", code, str(ROOT / "factory_hmi/preview_assets.py"), str(root)],
                            env=dict(os.environ, PYTHONPATH=str(runtime), PYTHONDONTWRITEBYTECODE="1"),
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "assets/app.js" in result.stdout and "assets/app.css" in result.stdout
