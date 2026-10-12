"""VALORANT のメンテナンス・障害ステータス監視。

status.riotgames.com のページが実際に叩いている非公式の公開 JSON を使う。
認証は要らない。ダウンストリームには Discord Webhook で通知する。

前回取得した内容を保管庫の外 (app_dir 直下、平文) に残しておき、次回との
差分で「新規発生」「解消」を判定する。アカウントや資格情報とは無関係の
公開情報なので、暗号化保管庫に入れる必要はない。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

STATUS_URL_TMPL = "https://valorant.secure.dyn.riotcdn.net/channels/public/x/status/{region}.json"


class StatusError(Exception):
    pass


JST = timezone(timedelta(hours=9))
_JST_TIME = re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})\s*JST")


@dataclass
class StatusSnapshot:
    maintenances: dict[str, dict] = field(default_factory=dict)
    incidents: dict[str, dict] = field(default_factory=dict)
    taken_at: float = 0.0   # 取得した時刻 (epoch 秒)。予定→実施中の判定に使う

    @property
    def active(self) -> bool:
        return bool(self.maintenances or self.incidents)


@dataclass
class StatusEvent:
    id: str
    kind: str      # "maintenance" | "incident"
    action: str    # "scheduled" (メンテ予定の発表) | "started" | "resolved"
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
        taken_at=time.time(),
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
        taken_at=float(data.get("taken_at") or 0.0),
    )


def save_snapshot(path: Path, snapshot: StatusSnapshot) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"maintenances": snapshot.maintenances, "incidents": snapshot.incidents,
               "taken_at": snapshot.taken_at}
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


def scheduled_start(entry: dict, locale: str = "ja_JP") -> datetime | None:
    """メンテナンスの開始予定時刻。タイトルか最新の更新文から "2026/10/14 06:00 JST"
    形式を探す。機械可読な開始時刻はステータス JSON に無い。"""
    for text in (_localized(entry, "titles", locale), _latest_update_text(entry, locale)):
        m = _JST_TIME.search(text or "")
        if m:
            y, mo, d, h, mi = (int(g) for g in m.groups())
            try:
                return datetime(y, mo, d, h, mi, tzinfo=JST)
            except ValueError:
                return None
    return None


def is_upcoming(entry: dict, now: float | None = None, locale: str = "ja_JP") -> bool:
    """まだ始まっていない (＝予定の) メンテナンスか。

    Riot は予定を告知した時点から maintenance_status を in_progress にする
    ので、その値は当てにならない (実機で確認: 開始の数日前から in_progress)。
    開始時刻が読めればそれと現在時刻を比べ、読めなければ status に従う。
    """
    start = scheduled_start(entry, locale)
    if start is not None:
        current = datetime.fromtimestamp(now if now is not None else time.time(), JST)
        return current < start
    return entry.get("maintenance_status") == "scheduled"


def diff(previous: StatusSnapshot, current: StatusSnapshot,
         locale: str = "ja_JP", now: float | None = None) -> list[StatusEvent]:
    """前回と今回を比べ、新規発生・解消したイベントを返す。

    メンテナンスは「予定の発表」「実際の開始」「終了」を区別する。
    """
    now = time.time() if now is None else now
    events: list[StatusEvent] = []

    for eid, entry in current.maintenances.items():
        if eid not in previous.maintenances:
            action = "scheduled" if is_upcoming(entry, now, locale) else "started"
            events.append(_event("maintenance", action, entry, locale))
        elif (previous.taken_at
              and is_upcoming(previous.maintenances[eid], previous.taken_at, locale)
              and not is_upcoming(entry, now, locale)):
            events.append(_event("maintenance", "started", entry, locale))
    for eid, entry in previous.maintenances.items():
        if eid not in current.maintenances:
            events.append(_event("maintenance", "resolved", entry, locale))

    for eid, entry in current.incidents.items():
        if eid not in previous.incidents:
            events.append(_event("incident", "started", entry, locale))
    for eid, entry in previous.incidents.items():
        if eid not in current.incidents:
            events.append(_event("incident", "resolved", entry, locale))
    return events


def _banner_title(entry: dict, locale: str) -> str:
    title = _localized(entry, "titles", locale)
    # "メンテナンス：2026/10/14 06:00 JST" は見出しで言うので頭を落とす
    return re.sub(r"^メンテナンス\s*[:：]\s*", "", title)


def summarize(current: StatusSnapshot, locale: str = "ja_JP",
              now: float | None = None) -> str:
    """バナー表示用の短い要約。アクティブな案件が無ければ空文字。"""
    parts = []
    upcoming = [e for e in current.maintenances.values() if is_upcoming(e, now, locale)]
    ongoing = [e for e in current.maintenances.values() if not is_upcoming(e, now, locale)]
    if upcoming:
        titles = [_banner_title(e, locale) for e in upcoming]
        parts.append("メンテナンス予定: " + " / ".join(t for t in titles if t))
    if ongoing:
        titles = [_banner_title(e, locale) for e in ongoing]
        parts.append("メンテナンス中: " + " / ".join(t for t in titles if t))
    if current.incidents:
        titles = [_localized(e, "titles", locale) for e in current.incidents.values()]
        parts.append("障害発生中: " + " / ".join(t for t in titles if t))
    return "　".join(parts)


# incident_severity の正式な値は info/warning/critical の3種のみ
# (Riot 公式 VAL-STATUS-V1 のスキーマに明記されている)。
SEVERITY_LABELS = {"info": "軽微", "warning": "警告", "critical": "重大"}


def notify_discord(webhook_url: str, event: StatusEvent, timeout: float = 10.0,
                    mention: str = "") -> None:
    if event.kind == "maintenance":
        heading = {"scheduled": "メンテナンス予定", "started": "メンテナンス開始",
                   "resolved": "メンテナンス終了"}[event.action]
    else:
        heading = "障害" + ("発生/開始" if event.action == "started" else "終了/復旧")
    lines = []
    if mention:
        lines.append(mention)
    lines.append(f"**VALORANT {heading}**")
    lines.append(event.title)
    if event.severity:
        lines.append(f"重大度: {SEVERITY_LABELS.get(event.severity, event.severity)}")
    if event.detail and event.detail != event.title:
        lines.append(event.detail)
    try:
        resp = requests.post(webhook_url, json={"content": "\n".join(lines)}, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise StatusError(f"Discord への通知に失敗しました: {exc}") from exc
