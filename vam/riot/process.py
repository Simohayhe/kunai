"""Riot 系プロセスの監視と終了。

セッションファイルを差し替える前に Riot Client を完全に落とす必要がある。
起動したままファイルを書き換えても、終了時にクライアントが上書きして元に戻す。
"""
from __future__ import annotations

import os
import time

import psutil

from .. import paths

# モック環境で動いているときに終了させたことにするプロセス名。
# FakeRiotEnv が差し込む。
_MOCK_RUNNING: set[str] = set()


def mock_mode() -> bool:
    """paths がモック環境に差し替えられているか。

    モックを指しているのに本物の Riot Client を kill してしまうと、
    テストのつもりが実際のゲームやダウンロードを巻き込む。
    そうならないよう、モック時は実プロセスに一切触れない。
    """
    return bool(os.environ.get(paths.ENV_OVERRIDE_LOCALAPPDATA))

# 落とす順番が大事。UX (画面) を先に、サービス本体を最後に。
# 実機 (Riot Client 134.x) を測ったところ、UI は Electron 化していて
# プロセス名は "Riot Client.exe"。これを落とし損ねると画面が居座り、
# セッションファイルを掴んだまま終了時に書き戻してしまう。
# 旧版の名前も残してあるので、どちらの版でも効く。
CLIENT_PROCESSES = (
    "Riot Client.exe",           # Electron 版の UI (実機で確認)
    "RiotClientUxRender.exe",    # 旧版
    "RiotClientUx.exe",          # 旧版
    "RiotClientCrashHandler.exe",
    "RiotClientServices.exe",
)
GAME_PROCESSES = (
    "VALORANT-Win64-Shipping.exe",
    "VALORANT.exe",
)
# Vanguard は落とさない。カーネルドライバなので触ると再起動が必要になる。


def _iter(names: tuple[str, ...]):
    lowered = {n.lower() for n in names}
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info["name"] or "").lower() in lowered:
                yield p
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


def running(names: tuple[str, ...] | None = None) -> list[str]:
    names = names or (CLIENT_PROCESSES + GAME_PROCESSES)
    if mock_mode():
        return sorted(n for n in _MOCK_RUNNING if n in names)
    return sorted({p.info["name"] for p in _iter(names)})


def game_running() -> bool:
    return bool(running(GAME_PROCESSES))


def client_running() -> bool:
    return bool(running(CLIENT_PROCESSES))


def stop_all(timeout: float = 8.0, include_game: bool = True) -> list[str]:
    """Riot Client (と任意でゲーム本体) を終了させ、終了させた名前を返す。

    まず terminate で行儀よく頼み、粘るものだけ kill する。
    """
    order = (GAME_PROCESSES + CLIENT_PROCESSES) if include_game else CLIENT_PROCESSES

    if mock_mode():
        # モック環境では実プロセスに触らない。終了したことにするだけ。
        stopped = [n for n in order if n in _MOCK_RUNNING]
        _MOCK_RUNNING.difference_update(stopped)
        return stopped

    stopped: list[str] = []
    deadline = time.time() + timeout

    for name in order:
        procs = list(_iter((name,)))
        if not procs:
            continue
        stopped.append(name)
        for p in procs:
            try:
                p.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        remaining = max(0.5, deadline - time.time())
        gone, alive = psutil.wait_procs(procs, timeout=min(2.0, remaining))
        for p in alive:
            try:
                p.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        psutil.wait_procs(alive, timeout=2.0)

    # ファイルハンドルが解放されるまでの猶予
    while time.time() < deadline and running(order):
        time.sleep(0.2)
    return stopped
