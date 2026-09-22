"""VALORANT のメンテナンス・障害ステータス監視。

status.riotgames.com のページが実際に叩いている非公式の公開 JSON を使う。
認証は要らない。ダウンストリームには Discord Webhook で通知する。

前回取得した内容を保管庫の外 (app_dir 直下、平文) に残しておき、次回との
差分で「新規発生」「解消」を判定する。アカウントや資格情報とは無関係の
公開情報なので、暗号化保管庫に入れる必要はない。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import requests

STATUS_URL_TMPL = "https://valorant.secure.dyn.riotcdn.net/channels/public/x/status/{region}.json"


class StatusError(Exception):
    pass


@dataclass
class StatusSnapshot:
    maintenances: dict[str, dict] = field(default_factory=dict)
    incidents: dict[str, dict] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return bool(self.maintenances or self.incidents)


@dataclass
class StatusEvent:
    id: str
    kind: str      # "maintenance" | "incident"
    action: str    # "started" | "resolved"
    title: str
    detail: str
    severity: str | None = None


def fetch(region: str = "ap", timeout: float = 10.0) -> StatusSnapshot:
    try:
        resp = requests.get(STATUS_URL_TMPL.format(region=region), timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise StatusError(f"ステータスを取得できませんでした: {exc}") from exc
    return StatusSnapshot(
        maintenances={str(e["id"]): e for e in data.get("maintenances", [])},
        incidents={str(e["id"]): e for e in data.get("incidents", [])},
    )


def load_snapshot(path: Path) -> StatusSnapshot | None:
    """前回のスナップショットを読む。無ければ None (=初回)。"""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return StatusSnapshot(
        maintenances=data.get("maintenances", {}),
        incidents=data.get("incidents", {}),
    )


def save_snapshot(path: Path, snapshot: StatusSnapshot) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"maintenances": snapshot.maintenances, "incidents": snapshot.incidents}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _localized(entry: dict, key: str, locale: str, fallback: str = "en_US") -> str:
    items = entry.get(key) or []
    for t in items:
        if t.get("locale") == locale:
            return t.get("content", "")
    for t in items:
        if t.get("locale") == fallback:
            return t.get("content", "")
    return ""


def _latest_update_text(entry: dict, locale: str, fallback: str = "en_US") -> str:
    updates = entry.get("updates") or []
    if not updates:
        return ""
    return _localized(updates[-1], "translations", locale, fallback)


def _event(kind: str, action: str, entry: dict, locale: str) -> StatusEvent:
    return StatusEvent(
        id=str(entry.get("id", "")),
        kind=kind,
        action=action,
        title=_localized(entry, "titles", locale) or "(no title)",
        detail=_latest_update_text(entry, locale),
        severity=entry.get("incident_severity"),
    )


def diff(previous: StatusSnapshot, current: StatusSnapshot,
         locale: str = "ja_JP") -> list[StatusEvent]:
    """前回と今回を比べ、新規発生・解消したイベントを返す。"""
    events: list[StatusEvent] = []
    for kind, prev_map, cur_map in (
        ("maintenance", previous.maintenances, current.maintenances),
        ("incident", previous.incidents, current.incidents),
    ):
        for eid, entry in cur_map.items():
            if eid not in prev_map:
                events.append(_event(kind, "started", entry, locale))
        for eid, entry in prev_map.items():
            if eid not in cur_map:
                events.append(_event(kind, "resolved", entry, locale))
    return events


def summarize(current: StatusSnapshot, locale: str = "ja_JP") -> str:
    """バナー表示用の短い要約。アクティブな案件が無ければ空文字。"""
    parts = []
    if current.maintenances:
        titles = [_localized(e, "titles", locale) for e in current.maintenances.values()]
        parts.append("メンテナンス中: " + " / ".join(t for t in titles if t))
    if current.incidents:
        titles = [_localized(e, "titles", locale) for e in current.incidents.values()]
        parts.append("障害発生中: " + " / ".join(t for t in titles if t))
    return "　".join(parts)


def notify_discord(webhook_url: str, event: StatusEvent, timeout: float = 10.0) -> None:
    label = "メンテナンス" if event.kind == "maintenance" else "障害"
    action = "発生/開始" if event.action == "started" else "終了/復旧"
    lines = [f"**VALORANT {label}{action}**", event.title]
    if event.severity:
        lines.append(f"重大度: {event.severity}")
    if event.detail and event.detail != event.title:
        lines.append(event.detail)
    try:
        resp = requests.post(webhook_url, json={"content": "\n".join(lines)}, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise StatusError(f"Discord への通知に失敗しました: {exc}") from exc
