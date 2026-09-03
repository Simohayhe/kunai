"""Riot 系プロセスの監視と終了。

セッションファイルを差し替える前に Riot Client を完全に落とす必要がある。
起動したままファイルを書き換えても、終了時にクライアントが上書きして元に戻す。
"""
from __future__ import annotations

import time

import psutil

# 落とす順番が大事。UX (画面) を先に、サービス本体を最後に。
CLIENT_PROCESSES = (
    "RiotClientUxRender.exe",
    "RiotClientUx.exe",
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
    return sorted({p.info["name"] for p in _iter(names)})


def game_running() -> bool:
    return bool(running(GAME_PROCESSES))


def client_running() -> bool:
    return bool(running(CLIENT_PROCESSES))


def stop_all(timeout: float = 12.0, include_game: bool = True) -> list[str]:
    """Riot Client (と任意でゲーム本体) を終了させ、終了させた名前を返す。

    まず terminate で行儀よく頼み、粘るものだけ kill する。
    """
    order = (GAME_PROCESSES + CLIENT_PROCESSES) if include_game else CLIENT_PROCESSES
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
        gone, alive = psutil.wait_procs(procs, timeout=min(4.0, remaining))
        for p in alive:
            try:
                p.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        psutil.wait_procs(alive, timeout=3.0)

    # ファイルハンドルが解放されるまでの猶予
    while time.time() < deadline and running(order):
        time.sleep(0.3)
    return stopped
