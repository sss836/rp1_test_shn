"""Dependency-free validation shared by preview serving and release packaging."""
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit


def _asset_references(document: str) -> list[str]:
    # This reads generated Vite entry tags, not arbitrary HTML. Avoid html.parser:
    # existing trimmed runtimes shadow stdlib html with an incomplete package.
    document = re.sub(r"<!--.*?-->", "", document, flags=re.DOTALL)
    references = []
    for match in re.finditer(r"<(script|link)\b([^>]*?)>", document, flags=re.IGNORECASE):
        tag, attributes = match.groups()
        attrs = {}
        for attr in re.finditer(r"([\w-]+)\s*=\s*([\"'])(.*?)\2", attributes, flags=re.DOTALL):
            attrs[attr.group(1).lower()] = attr.group(3)
        if tag.lower() == "script" and attrs.get("src"):
            references.append(attrs["src"])
        if tag.lower() == "link" and attrs.get("rel", "").lower() in {"stylesheet", "modulepreload"}:
            references.append(attrs.get("href", ""))
    return references


def validate_preview_assets(root: Path) -> list[str]:
    """Reject missing/partial builds, remote assets and paths outside assets/."""
    try:
        references = _asset_references((root / "index.html").read_text(encoding="utf-8"))
        files = []
        for reference in references:
            url = urlsplit(reference)
            path = unquote(url.path)
            if url.scheme or url.netloc or not path.startswith("/preview/assets/"):
                raise ValueError("unexpected preview asset URL")
            relative = path.removeprefix("/preview/")
            asset = (root / relative).resolve()
            if not asset.is_relative_to((root / "assets").resolve()):
                raise ValueError("preview asset escapes assets directory")
            if not asset.is_file() or asset.stat().st_size == 0:
                raise ValueError("preview asset missing or empty: " + relative)
            files.append(relative)
        if not any(name.endswith(".js") for name in files) or not any(name.endswith(".css") for name in files):
            raise ValueError("preview entry must reference built JavaScript and CSS")
        return files
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(
            "本机曲线预览资源缺失或不完整，请安装包含完整预览资源的修复包；"
            "构建时先在 frontend 执行 npm run build:preview。"
        ) from exc
