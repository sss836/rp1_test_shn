"""Shell-safe software download and local package installation commands."""
from __future__ import annotations

import re
import shlex
from pathlib import Path
from urllib.parse import urlsplit


def package_install_command(package_path: str) -> str:
    """Copy an apt command for the staged receipt path; never execute it."""
    if (not isinstance(package_path, str) or not package_path
            or any(ord(char) < 32 or ord(char) == 127 for char in package_path)
            or not Path(package_path).is_absolute()
            or Path(package_path).suffix.lower() != ".deb"):
        raise ValueError("下载回执缺少有效的 DEB 安装包绝对路径，请重新下载。")
    return shlex.join(["sudo", "apt", "install", package_path])


# Use the already authenticated gateway instead of putting a platform cookie or
# password on the clipboard. Only the existing software download route is used.
DOWNLOAD_SCRIPT = '''import getpass, json, os, sys
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

def download(token):
    headers = {"X-RP1-Operator-Token": token} if token else {}
    request = Request(sys.argv[1], data=b"", headers=headers, method="POST")
    with build_opener(NoRedirect()).open(request, timeout=1800) as response:
        raw = response.read(131073)
    if len(raw) > 131072:
        raise ValueError("下载回执过大")
    payload = json.loads(raw)
    result = payload.get("result") or {}
    if payload.get("ok") is not True or result.get("status") != "staged":
        raise ValueError("未取得已校验的下载回执")
    path = result.get("package_path")
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ValueError("下载回执缺少安装包的绝对路径")
    print("安装包已下载，签名和 SHA256 已校验。")
    print("保存位置（上位机网关所在电脑）：\\n" + path)
    print("仅下载，尚未安装。")

try:
    token = os.environ.get("RP1_FACTORY_OPERATOR_TOKEN", "").strip()
    try:
        download(token)
    except HTTPError as error:
        if error.code != 401 or token or not sys.stdin.isatty():
            raise
        token = getpass.getpass("网关操作令牌（不回显；不是平台密码）：")
        download(token)
except HTTPError as error:
    if error.code == 401:
        message = "网关操作令牌无效或缺失，请在终端安全输入或设置 RP1_FACTORY_OPERATOR_TOKEN。"
    elif error.code == 409:
        message = "请在上位机登录执行员或管理员账号，重新检查版本后再下载；也可能有其他下载正在进行。"
    else:
        message = "请确认上位机正在运行，且该版本仍可下载。"
    sys.exit("下载失败（HTTP %s）：%s" % (error.code, message))
except (OSError, URLError, ValueError, TypeError, AttributeError) as error:
    sys.exit("下载失败，请检查上位机连接及下载回执：" + str(error))
'''


def gateway_download_command(base_url: str, release_id: str) -> str:
    """Return a shell-safe command without copying any in-memory credentials."""
    if not re.fullmatch(r"[0-9a-f]{32}", release_id):
        raise ValueError("更新发布编号无效，请重新检查更新。")
    parsed = urlsplit(base_url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment
            or any(ord(char) < 32 or ord(char) == 127 for char in base_url)):
        raise ValueError("网关地址无效，不能生成下载命令。")
    # Accessing port also rejects malformed values before anything is copied.
    parsed.port
    url = base_url.rstrip("/") + f"/api/v1/software-updates/{release_id}/download"
    return shlex.join(["python3", "-c", DOWNLOAD_SCRIPT, url])
