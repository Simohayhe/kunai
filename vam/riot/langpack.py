"""VALORANT の音声とテキストの言語を別々にする (言語ファイルの差し替え)。

⚠ 公式にサポートされていない方法。ゲームのファイルを書き換えるため、Vanguard に
  検知されてアカウントが停止 (BAN) される可能性がある。使うかどうかは自己責任。

仕組み (コミュニティの Valorant Language Changer と同じ考え方):
  1. 音声にしたい言語 (voice) とテキストにしたい言語 (text) の
     `<言語>_Text-WindowsClient.pak / .sig` を、Riot の公式配布サーバーから取得して
     Kunai のフォルダに置く。
  2. 起動の前に、voice 言語のテキストファイルを正しい状態に戻しておく
     (Riot Client のファイル検査を通すため)。
  3. VALORANT を起動し、ゲームのプロセスが立ち上がった直後に、
     voice 言語のテキストファイルを text 言語のもので上書きする。
  音声ファイル (`*_Audio-*`) は触らない。だから音声は voice 言語のまま、
  文字だけ text 言語になる。

取得には、Riot が公開しているパッチのマニフェスト (RMAN 形式) を読み、
バンドルから必要なチャンクだけを HTTP Range で取る。ManifestDownloader
(Morilli) の読み方を参考にした Python 実装で、外部の実行ファイルは使わない。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from .. import diagnostics

CONFIG_URL = ("https://clientconfig.rpg.riotgames.com/api/v1/config/public"
              "?namespace=keystone.products.valorant.patchlines")
BUNDLE_BASE = "https://valorant.secure.dyn.riotcdn.net/channels/public/bundles"
PAKS_SUBDIR = Path("ShooterGame") / "Content" / "Paks"
TEXT_SUFFIXES = ("Text-WindowsClient.pak", "Text-WindowsClient.sig")

LOCALE_LABELS = {
    "en_US": "英語", "ja_JP": "日本語", "ko_KR": "韓国語", "zh_TW": "中国語 (繁体)",
    "zh_CN": "中国語 (簡体)", "de_DE": "ドイツ語", "fr_FR": "フランス語",
    "es_ES": "スペイン語 (スペイン)", "es_MX": "スペイン語 (メキシコ)",
    "it_IT": "イタリア語", "pt_BR": "ポルトガル語", "ru_RU": "ロシア語",
    "pl_PL": "ポーランド語", "tr_TR": "トルコ語", "th_TH": "タイ語",
    "vi_VN": "ベトナム語", "id_ID": "インドネシア語", "ar_AE": "アラビア語",
}


class LangPackError(Exception):
    pass


# ---------------------------------------------------------------------------
# 場所
# ---------------------------------------------------------------------------
def store_dir(app_dir: Path) -> Path:
    return Path(app_dir) / "langpacks"


def _program_data() -> Path:
    return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))


def product_settings_path() -> Path:
    return (_program_data() / "Riot Games" / "Metadata" / "valorant.live"
            / "valorant.live.product_settings.yaml")


def _read_product_settings() -> dict:
    try:
        import yaml
        data = yaml.safe_load(product_settings_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def find_paks_dir() -> Path | None:
    """VALORANT の Paks フォルダ。Riot の製品設定にあるインストール先から探す。"""
    install = _read_product_settings().get("product_install_full_path")
    candidates = []
    if install:
        candidates.append(Path(str(install)) / PAKS_SUBDIR)
    candidates.append(Path("C:/Riot Games/VALORANT/live") / PAKS_SUBDIR)
    for path in candidates:
        if path.is_dir():
            return path
    return None


def available_locales() -> list[str]:
    """このインストールで選べる言語。製品設定にあればそれ、無ければ既知の一覧。"""
    locales = (_read_product_settings().get("locale_data") or {}).get("available_locales")
    if isinstance(locales, list) and locales:
        return [str(x) for x in locales]
    return list(LOCALE_LABELS)


# ---------------------------------------------------------------------------
# RMAN マニフェスト (FlatBuffers を必要な分だけ読む)
# ---------------------------------------------------------------------------
HASH_SHA256, HASH_HKDF, HASH_BLAKE3 = 2, 3, 4


def _hkdf64(data: bytes) -> int:
    """チャンク ID の HKDF 形式ハッシュ (ManifestDownloader の実装に合わせる)。"""
    key = hashlib.sha256(data).digest() + bytes(32)          # 64 バイトに 0 埋め
    ipad = bytes(b ^ 0x36 for b in key)
    opad = bytes(b ^ 0x5C for b in key)

    def step(msg: bytes) -> bytes:
        return hashlib.sha256(opad + hashlib.sha256(ipad + msg).digest()).digest()

    buffer = step(b"\x00\x00\x00\x01")
    result = bytearray(buffer[:8])
    for _ in range(31):
        buffer = step(buffer)
        for i in range(8):
            result[i] ^= buffer[i]
    return struct.unpack("<Q", bytes(result))[0]


def chunk_hash_ok(data: bytes, chunk_id: int, hash_type: int) -> bool | None:
    """チャンクの検証。確認できない種類 (BLAKE3 等) は None を返す。"""
    if hash_type == HASH_SHA256:
        return struct.unpack("<Q", hashlib.sha256(data).digest()[:8])[0] == chunk_id
    if hash_type == HASH_HKDF:
        return _hkdf64(data) == chunk_id
    return None


class _Table:
    """FlatBuffers の 1 テーブル。vtable でフィールドの位置を引く。"""

    def __init__(self, buf: bytes, pos: int):
        self.buf = buf
        self.pos = pos
        vt = pos - struct.unpack_from("<i", buf, pos)[0]
        self.vt = vt
        self.vt_size = struct.unpack_from("<H", buf, vt)[0]

    def field_pos(self, index: int) -> int | None:
        if (self.vt_size - 4) // 2 <= index:
            return None
        off = struct.unpack_from("<H", self.buf, self.vt + 4 + 2 * index)[0]
        return self.pos + off if off else None

    def scalar(self, index: int, fmt: str, default=0):
        p = self.field_pos(index)
        return struct.unpack_from(fmt, self.buf, p)[0] if p is not None else default

    def _indirect(self, index: int) -> int | None:
        p = self.field_pos(index)
        return p + struct.unpack_from("<I", self.buf, p)[0] if p is not None else None

    def string(self, index: int) -> str | None:
        p = self._indirect(index)
        if p is None:
            return None
        n = struct.unpack_from("<I", self.buf, p)[0]
        return self.buf[p + 4:p + 4 + n].decode("utf-8", "replace")

    def tables(self, index: int) -> list["_Table"]:
        p = self._indirect(index)
        if p is None:
            return []
        n = struct.unpack_from("<I", self.buf, p)[0]
        out = []
        for i in range(n):
            ep = p + 4 + 4 * i
            out.append(_Table(self.buf, ep + struct.unpack_from("<I", self.buf, ep)[0]))
        return out

    def u64_vector(self, index: int) -> list[int]:
        p = self._indirect(index)
        if p is None:
            return []
        n = struct.unpack_from("<I", self.buf, p)[0]
        return list(struct.unpack_from(f"<{n}Q", self.buf, p + 4))


@dataclass
class ChunkRef:
    chunk_id: int
    bundle_id: int
    bundle_offset: int
    compressed_size: int
    uncompressed_size: int


@dataclass
class ManifestFile:
    name: str
    size: int
    chunks: list[ChunkRef]
    hash_type: int


def parse_manifest(data: bytes) -> dict[str, ManifestFile]:
    """RMAN マニフェストを読み、ファイル名 (小文字) -> ManifestFile を返す。"""
    if data[:4] != b"RMAN":
        raise LangPackError("マニフェストの形式が違います (RMAN ではありません)")
    if data[4] != 2:
        raise LangPackError(f"未対応のマニフェスト版です ({data[4]}.{data[5]})")
    try:
        import zstandard
    except ImportError as exc:
        raise LangPackError("zstandard が入っていないため、言語ファイルを取得できません") from exc
    offset, compressed, _mid, uncompressed = struct.unpack_from("<IIQI", data, 8)
    # 本文は複数の zstd フレームに分かれていることがあるので、フレームをまたいで読む
    dobj = zstandard.ZstdDecompressor().decompressobj(read_across_frames=True)
    body = dobj.decompress(data[offset:offset + compressed])
    if len(body) != uncompressed:
        raise LangPackError("マニフェストの展開サイズが想定と違います")
    root = _Table(body, struct.unpack_from("<I", body, 0)[0])

    chunks: dict[int, ChunkRef] = {}
    for bundle in root.tables(0):
        bundle_id = bundle.scalar(0, "<Q")
        pos = 0
        for chunk in bundle.tables(1):
            ref = ChunkRef(
                chunk_id=chunk.scalar(0, "<Q"), bundle_id=bundle_id, bundle_offset=pos,
                compressed_size=chunk.scalar(1, "<I"), uncompressed_size=chunk.scalar(2, "<I"),
            )
            pos += ref.compressed_size
            chunks[ref.chunk_id] = ref

    directories = {}
    for d in root.tables(3):
        directories[d.scalar(0, "<Q")] = (d.scalar(1, "<Q"), d.string(2) or "")
    hash_types = [p.scalar(1, "<B") for p in root.tables(5)]

    files: dict[str, ManifestFile] = {}
    for entry in root.tables(2):
        parts = [entry.string(3) or ""]
        directory_id = entry.scalar(1, "<Q")
        while directory_id:
            directory_id, name = directories[directory_id]
            parts.append(name)
        full = "/".join(reversed(parts))
        refs = [chunks[cid] for cid in entry.u64_vector(7) if cid in chunks]
        param = entry.scalar(11, "<B")
        files[full.lower()] = ManifestFile(
            name=full, size=entry.scalar(2, "<Q"), chunks=refs,
            hash_type=hash_types[param] if param < len(hash_types) else 0,
        )
    return files


# ---------------------------------------------------------------------------
# 取得
# ---------------------------------------------------------------------------
def _get(url: str, headers: dict | None = None, timeout: float = 30.0) -> requests.Response:
    try:
        resp = requests.get(url, headers=headers or {}, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise LangPackError(f"Riot のサーバーから取得できませんでした: {exc}") from exc
    return resp


def current_manifest_url(region: str = "ap") -> str:
    """いまの VALORANT (live) のパッチマニフェストの URL。"""
    try:
        data = _get(CONFIG_URL).json()
        configs = (data["keystone.products.valorant.patchlines.live"]
                   ["platforms"]["win"]["configurations"])
    except (KeyError, ValueError) as exc:
        raise LangPackError("パッチ情報の形式が想定と違います") from exc
    for config in configs:
        if config.get("id") == region and config.get("patch_url"):
            return config["patch_url"]
    for config in configs:   # 地域が見つからなければ、どれでも同じ版のはず
        if config.get("patch_url"):
            return config["patch_url"]
    raise LangPackError("パッチのマニフェストが見つかりませんでした")


def download_file(entry: ManifestFile, dest: Path, bundle_base: str = BUNDLE_BASE) -> None:
    """マニフェストのファイル 1 つを取得して dest に書く (一時ファイル経由)。"""
    try:
        import zstandard
    except ImportError as exc:
        raise LangPackError("zstandard が入っていないため、言語ファイルを取得できません") from exc
    decompressor = zstandard.ZstdDecompressor()
    out = bytearray(entry.size)
    offsets, pos = {}, 0
    for ref in entry.chunks:
        offsets[ref.chunk_id] = pos
        pos += ref.uncompressed_size

    # 同じバンドルの中で隣り合うチャンクは、1 回の Range 取得にまとめる
    ordered = sorted(entry.chunks, key=lambda r: (r.bundle_id, r.bundle_offset))
    groups: list[list[ChunkRef]] = []
    for ref in ordered:
        last = groups[-1][-1] if groups else None
        if (last and last.bundle_id == ref.bundle_id
                and last.bundle_offset + last.compressed_size == ref.bundle_offset):
            groups[-1].append(ref)
        else:
            groups.append([ref])

    for group in groups:
        start = group[0].bundle_offset
        end = group[-1].bundle_offset + group[-1].compressed_size - 1
        resp = _get(f"{bundle_base}/{group[0].bundle_id:016X}.bundle",
                    headers={"Range": f"bytes={start}-{end}"})
        blob = resp.content
        if len(blob) != end - start + 1:
            raise LangPackError("バンドルの取得サイズが想定と違います")
        for ref in group:
            raw = blob[ref.bundle_offset - start:ref.bundle_offset - start + ref.compressed_size]
            data = decompressor.decompress(raw, max_output_size=ref.uncompressed_size)
            if len(data) != ref.uncompressed_size:
                raise LangPackError("展開したサイズが想定と違います")
            if chunk_hash_ok(data, ref.chunk_id, entry.hash_type) is False:
                raise LangPackError(f"チャンクの検証に失敗しました: {entry.name}")
            off = offsets[ref.chunk_id]
            out[off:off + len(data)] = data

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(bytes(out))
    os.replace(tmp, dest)


def pack_files(locale: str) -> list[str]:
    return [f"{locale}_{s}" for s in TEXT_SUFFIXES]


def packs_present(store: Path, locales) -> bool:
    return all((store / name).is_file() for loc in locales for name in pack_files(loc))


def ensure_packs(store: Path, locales, region: str = "ap", progress=lambda m: None,
                 force: bool = False) -> str:
    """言語ファイルを取得して store に置く。版 (マニフェスト URL) が変わっていたら取り直す。
    返り値は、使ったマニフェストの URL。"""
    store.mkdir(parents=True, exist_ok=True)
    state_path = store / "state.json"
    progress("パッチ情報を確認中…")
    url = current_manifest_url(region)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    locales = sorted(set(locales))
    if (not force and state.get("manifest_url") == url
            and set(locales) <= set(state.get("locales") or []) and packs_present(store, locales)):
        progress("言語ファイルは最新です")
        return url

    progress("マニフェストを取得中…")
    files = parse_manifest(_get(url).content)
    for loc in locales:
        for name in pack_files(loc):
            key = f"{PAKS_SUBDIR.as_posix()}/{name}".lower()
            entry = files.get(key)
            if entry is None:
                raise LangPackError(f"この言語は取得できませんでした: {loc}")
            progress(f"{name} を取得中…")
            download_file(entry, store / name)
    state_path.write_text(json.dumps({"manifest_url": url, "locales": locales}),
                          encoding="utf-8")
    return url


# ---------------------------------------------------------------------------
# 差し替え
# ---------------------------------------------------------------------------
def _copy(src: Path, dst: Path, retries: int = 20) -> None:
    for attempt in range(retries):
        try:
            shutil.copyfile(src, dst)
            return
        except PermissionError:
            if attempt == retries - 1:
                raise
            time.sleep(0.05)


def install_text(store: Path, paks: Path, source_locale: str, target_locale: str) -> None:
    """source_locale のテキストファイルを、target_locale の名前で paks に置く。"""
    for src_name, dst_name in zip(pack_files(source_locale), pack_files(target_locale)):
        _copy(store / src_name, paks / dst_name)


def voice_installed(paks: Path, voice: str) -> bool:
    """Riot Client のゲーム言語が voice になっている (voice の音声・テキストが入っている) か。"""
    return all((paks / name).is_file() for name in pack_files(voice))


def prepare_launch(store: Path, voice: str, text: str, region: str = "ap",
                   progress=lambda m: None) -> Path:
    """起動前の準備。言語ファイルを揃え、voice のテキストを元の状態に戻す。
    Paks フォルダを返す。問題があれば LangPackError。"""
    paks = find_paks_dir()
    if paks is None:
        raise LangPackError("VALORANT のインストール先が見つかりませんでした")
    if not voice_installed(paks, voice):
        raise LangPackError(
            f"Riot Client のゲーム言語を {voice} ({LOCALE_LABELS.get(voice, voice)}) にしてください。"
            "音声にしたい言語で VALORANT を一度インストール/更新しておく必要があります")
    ensure_packs(store, [voice, text], region, progress)
    install_text(store, paks, voice, voice)   # 元の (voice の) テキストに戻す
    return paks


def _game_running() -> bool:
    import psutil
    names = {"valorant.exe", "valorant-win64-shipping.exe"}
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info["name"] or "").lower() in names:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def swap_when_game_starts(store: Path, paks: Path, voice: str, text: str,
                          cancel_event: threading.Event | None = None,
                          timeout: float = 300.0, poll: float = 0.1,
                          is_running=_game_running) -> bool:
    """VALORANT のプロセスが立ち上がった直後に、voice のテキストを text のもので上書きする。
    間に合ったら True。タイムアウト / キャンセルなら False。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            return False
        if is_running():
            install_text(store, paks, text, voice)
            diagnostics.log(f"言語: {voice} のテキストを {text} に差し替えました")
            return True
        time.sleep(poll)
    return False


def start_swap_thread(store: Path, paks: Path, voice: str, text: str,
                      cancel_event: threading.Event | None = None) -> threading.Thread:
    def run() -> None:
        try:
            ok = swap_when_game_starts(store, paks, voice, text, cancel_event)
            if not ok:
                diagnostics.log("言語: ゲームの起動を確認できなかったので差し替えませんでした")
        except Exception as exc:  # 裏で動くので、失敗しても落とさずログに残す
            diagnostics.log(f"言語: 差し替えに失敗: {exc}")

    thread = threading.Thread(target=run, name="langpack-swap", daemon=True)
    thread.start()
    return thread


def restore_original(store: Path, voice: str) -> None:
    """差し替えたファイルを、voice 本来のテキストに戻す。"""
    paks = find_paks_dir()
    if paks is None or not packs_present(store, [voice]):
        raise LangPackError("元に戻すためのファイルがありません。Riot Client のゲーム修復をお使いください")
    install_text(store, paks, voice, voice)
