"""GitHub Releases を見て、新しい exe に入れ替える。

twitcasting-notifier (同じ作者の別アプリ) の updater.py と同じ方式に揃えてある。

このリポジトリは Private なので、リリースの取得には認証が要る (無認証だと 404)。
認証は次の順で探す:

1. `gh` CLI がログイン済みならそれを使う (このPCでは設定不要)
2. 環境変数 `GITHUB_TOKEN` / `GH_TOKEN`

どちらも無ければ「確認できない」とだけ返す。邪魔をしないよう、呼び側は
これを黙って諦める前提で使うこと。ソースから動かしている場合は
入れ替えようがないので、リリースページを開く案内だけを出す。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import requests

from .version import __version__

REPO = "Simohayhe/valorant-account-manager"
ASSET_NAME = "ValorantAccountManager.exe"
RELEASES_URL = f"https://github.com/{REPO}/releases"
API_BASE = f"https://api.github.com/repos/{REPO}"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --------------------------------------------------------------------------
# バージョン比較
# --------------------------------------------------------------------------
def parse_version(text: str) -> tuple[int, ...]:
    """'v1.2.3' や '1.2' を (1,2,3) / (1,2) にする。数字以外は無視する。"""
    numbers = re.findall(r"\d+", str(text or ""))
    return tuple(int(n) for n in numbers) or (0,)


def is_newer(candidate: str, current: str = __version__) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    # (1,2) と (1,2,0) を同じ扱いにするため長さを揃える
    length = max(len(a), len(b))
    a += (0,) * (length - len(a))
    b += (0,) * (length - len(b))
    return a > b


# --------------------------------------------------------------------------
# 認証手段さがし
# --------------------------------------------------------------------------
# gh がハングした (実機で確認済み: 稀に応答が返らないことがある) ときに
# 呼び側を巻き込んで無限に待たされないよう、必ず上限を付ける。
DEFAULT_TIMEOUT = 20.0


def _run(args: list[str], timeout: float = DEFAULT_TIMEOUT,
         **kwargs) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=_NO_WINDOW, timeout=timeout, **kwargs,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args, returncode=1, stdout="",
            stderr=f"{args[0]} の応答がありませんでした ({timeout:.0f} 秒)",
        )


def gh_ready() -> bool:
    """gh CLI があって、ログイン済みか。"""
    if not shutil.which("gh"):
        return False
    return _run(["gh", "auth", "status"]).returncode == 0


def find_token() -> str | None:
    for value in (os.environ.get("GITHUB_TOKEN"), os.environ.get("GH_TOKEN")):
        if value and value.strip():
            return value.strip()
    return None


# --------------------------------------------------------------------------
# リリースを見る
# --------------------------------------------------------------------------
@dataclass
class Release:
    tag: str
    name: str
    notes: str
    asset_url: str | None       # ダウンロード先。種類は asset_kind 次第
    asset_kind: str = "public"  # "public" (認証不要) / "api" (トークン) / "gh" (gh CLI)


class UpdateUnavailable(Exception):
    """更新の確認手段が無い。"""


def check_latest() -> Release:
    """リポジトリは Public なので、まず認証無しの公開 API を試す。

    これだけで完結すれば gh CLI のサブプロセス呼び出し自体を避けられる。
    実機で gh が稀に応答を返さないことを確認しているので、これが本命。
    見えない/失敗したときだけ、トークンや gh CLI にフォールバックする
    (リポジトリが再び Private になった場合の保険)。
    """
    try:
        return _latest_via_public_api()
    except requests.RequestException:
        pass

    token = find_token()
    if token:
        return _latest_via_api(token)
    if gh_ready():
        return _latest_via_gh()
    raise UpdateUnavailable(
        "リリース情報を取得できませんでした。ネットワーク接続を確認するか、"
        "gh CLI でログインしてください。"
    )


def _latest_via_public_api() -> Release:
    resp = requests.get(
        f"{API_BASE}/releases/latest",
        headers={"Accept": "application/vnd.github+json"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    asset = next(
        (a for a in data.get("assets", []) if a.get("name") == ASSET_NAME), None
    )
    return Release(
        tag=data.get("tag_name", ""), name=data.get("name", ""),
        notes=data.get("body", "") or "",
        asset_url=asset.get("browser_download_url") if asset else None,
        asset_kind="public",
    )


def _latest_via_api(token: str) -> Release:
    resp = requests.get(
        f"{API_BASE}/releases/latest",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    asset_url = next(
        (a["url"] for a in data.get("assets", []) if a.get("name") == ASSET_NAME), None
    )
    return Release(
        tag=data.get("tag_name", ""), name=data.get("name", ""),
        notes=data.get("body", "") or "", asset_url=asset_url, asset_kind="api",
    )


def _latest_via_gh() -> Release:
    proc = _run(
        ["gh", "release", "view", "--repo", REPO, "--json", "tagName,name,body,assets"]
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip() or "gh が失敗しました")
    data = json.loads(proc.stdout)
    has_asset = any(a.get("name") == ASSET_NAME for a in data.get("assets", []))
    return Release(
        tag=data.get("tagName", ""), name=data.get("name", ""),
        notes=data.get("body", "") or "",
        asset_url=("gh" if has_asset else None), asset_kind="gh",
    )


# --------------------------------------------------------------------------
# ダウンロード
# --------------------------------------------------------------------------
def download(release: Release, dest_dir: Path) -> Path:
    """新しい exe を落として、その置き場所を返す。"""
    if not release.asset_url:
        raise RuntimeError(f"リリース {release.tag} に {ASSET_NAME} が添付されていません。")

    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"{ASSET_NAME}.new"
    target.unlink(missing_ok=True)

    if release.asset_kind == "public":
        # 認証不要。browser_download_url に余計なヘッダを付けると
        # 署名付き URL の検証で弾かれることがあるので何も足さない。
        with requests.get(release.asset_url, timeout=120, stream=True) as resp:
            resp.raise_for_status()
            with target.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    f.write(chunk)
    elif release.asset_kind == "api":
        token = find_token()
        with requests.get(
            release.asset_url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/octet-stream"},
            timeout=120, stream=True,
        ) as resp:
            resp.raise_for_status()
            with target.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    f.write(chunk)
    else:
        # gh release download はファイル名を指定できないので、専用フォルダに落としてから移す
        staging = dest_dir / "_dl"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        proc = _run(
            ["gh", "release", "download", release.tag, "--repo", REPO,
             "--pattern", ASSET_NAME, "--dir", str(staging)],
            timeout=180,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "").strip() or "ダウンロードに失敗")
        shutil.move(str(staging / ASSET_NAME), target)
        shutil.rmtree(staging, ignore_errors=True)

    if target.stat().st_size < 1_000_000:
        raise RuntimeError("落としたファイルが小さすぎます。中断しました。")
    return target


# --------------------------------------------------------------------------
# 入れ替え
# --------------------------------------------------------------------------
HELPER = r"""
$ErrorActionPreference = 'SilentlyContinue'
$target  = '{target}'
$newFile = '{new_file}'
$myPid   = {pid}

# 1. 呼び出し元の GUI が終わるのを待つ (exe を掴んだままだと置き換えられない)
Wait-Process -Id $myPid -Timeout 30
$name = [System.IO.Path]::GetFileNameWithoutExtension($target)
for ($i = 0; $i -lt 12; $i++) {{
    $running = Get-Process -Name $name -ErrorAction SilentlyContinue
    if (-not $running) {{ break }}
    Start-Sleep -Milliseconds 500
}}
Get-Process -Name $name -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 500

# 2. 置き換える (掴まれている間は数回やり直す)
$done = $false
for ($i = 0; $i -lt 10; $i++) {{
    try {{
        Copy-Item -LiteralPath $newFile -Destination $target -Force -ErrorAction Stop
        $done = $true
        break
    }} catch {{ Start-Sleep -Milliseconds 700 }}
}}

if ($done) {{
    Remove-Item -LiteralPath $newFile -Force
    Start-Process -FilePath $target
}} else {{
    # 失敗したら手で置き換えられるようにファイルを残し、場所を知らせる
    Start-Process explorer.exe -ArgumentList ('/select,"' + $newFile + '"')
}}
"""


def apply_update(new_file: Path) -> None:
    """GUI を終了させてから入れ替えるための後始末スクリプトを起動する。

    Windows は実行中の exe を上書きできないので、「こちらが終わるのを待ってから
    置き換えて起動し直す」係を別プロセスで走らせる。
    呼び出した側は、これを呼んだらすぐ終了すること。
    """
    if not getattr(sys, "frozen", False):
        raise RuntimeError("ソースから動かしているときは入れ替えできません。")

    target = Path(sys.executable).resolve()
    script = HELPER.format(
        target=str(target).replace("'", "''"),
        new_file=str(new_file).replace("'", "''"),
        pid=os.getpid(),
    )
    helper_path = Path(tempfile.gettempdir()) / "vam_update.ps1"
    helper_path.write_text(script, encoding="utf-8-sig")

    # DETACHED_PROCESS を付けると powershell がそもそも起動しない (twitcasting-notifier で実測済み)。
    # CREATE_NO_WINDOW なら窓を出さずに動き、こちらが終了しても生き残る。
    subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(helper_path)],
        creationflags=_NO_WINDOW, close_fds=True,
    )
