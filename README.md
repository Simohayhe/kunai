# VALORANT Account Manager

複数の VALORANT アカウントを、パスワードを打ち直さずに切り替えるための Windows デスクトップアプリ。
ランク・ウォレット・所持スキンも、アカウントを切り替えずにまとめて確認できる。

![UI](docs/screenshot.png)

## できること

| 機能 | 説明 |
|---|---|
| アカウント切り替え | ログイン済みセッションを保存しておき、ワンクリックで差し替えて VALORANT を起動 |
| 自動ログイン | セッションが失効したときのフォールバック。ID→Tab→パスワード→サインインまでキーボードだけで行う |
| ランク管理 | 現在ランク・RR・勝率・最高ランクを全アカウント分まとめて取得 |
| 戦績 | 直近のコンペティティブ戦のランク変動履歴 |
| 所持品 | 所持スキンをレア度別に集計、エージェント／カード／タイトル等の所持数 |
| ウォレット | VP / RP / KC の残高 |
| 認証情報の保管 | ID・パスワードを暗号化して保存。右クリックからコピー |
| セッションの延長 | 再認証のたびに cookie を書き戻して有効期限を更新。残り日数を常時表示 |

## セキュリティ

- アカウント情報は **AES-256-GCM** で暗号化して 1 ファイルに保存する
- その鍵は **Windows DPAPI (CurrentUser)** で包む。**このWindowsユーザーでログインしている状態でしか復号できない**
- 任意で **マスターパスワード**（PBKDF2-HMAC-SHA256 / 60万回）を追加できる。設定すると、同じ Windows ユーザーでもパスワード無しでは開けない
- Riot のセッション cookie も同じ鍵で暗号化して保管する
- パスワードは平文ではどこにも書き出さない。ネットワークにも送らない

> マスターパスワードを忘れると保管庫は開けません。復旧手段はありません。

## 動作要件

- Windows 10 / 11
- VALORANT（Riot Client）がインストールされていること

## 導入

### exe を使う（Python 不要）

[Releases](../../releases) から `ValorantAccountManager.exe` を落として実行するだけ。
インストール不要。設定と保管庫は `%LOCALAPPDATA%\ValorantAccountManager` に作られる。

### ソースから動かす

Python 3.11 以上が必要。

```bash
pip install -r requirements.txt
python main.py
```

`run.cmd` をダブルクリックでも起動できる。

### 最初の登録

1. Riot Client で普通にログインする（**「ログイン情報を保存する」を必ず有効にする**）
2. 本アプリで **「現在のを取り込む」** を押す
3. 登録したいアカウントの分だけ、ログインし直して 1〜2 を繰り返す

セッションを保存できていれば、以降はパスワード不要で切り替えられる。

### 切り替え

アカウントをダブルクリック、または **「このアカウントで起動」**。内部では次の順で動く。

1. 現在ログイン中のアカウントが登録済みなら、そのセッションを退避
2. Riot Client と VALORANT を終了（Vanguard には触らない）
3. 対象アカウントのセッションファイルを書き戻す
4. `RiotClientServices.exe --launch-product=valorant --launch-patchline=live` で起動

セッションが失効していて、かつ ID/パスワードが登録済みなら、自動入力にフォールバックする。

### 自動入力の中身

ユーザー名 → Tab → パスワード → 「サインイン状態を維持」 → Enter。
**すべてキーボードで行い、座標クリックには頼らない。**

実機 (Riot Client v138.0.1) で確かめた要点:

- UI は **Electron**。ウィンドウクラスは `Chrome_WidgetWin_1`、プロセス名は `Riot Client.exe`
- `Chrome_WidgetWin_1` は Chromium 系アプリの汎用クラスなので、**クラスだけで判定してはいけない**。
  所有プロセスが Riot のものかで絞っている
- **`SetForegroundWindow` を呼ぶだけでは「最前面に出るがアクティブでない」状態になることがある。**
  そうなると Riot Client は画面を暗転させたままで、キー入力が 1 文字も届かない。
  ALT の空打ちと `AttachThreadInput` で確実に活性化してから打つ
- 正しくアクティブ化できていれば、起動直後のログイン画面はユーザー名欄に
  フォーカスが載っているので、クリックは要らない
- 起動直後はスプラッシュ (600x600) が出て、読み込み後にウィンドウが作り直される。
  古いハンドルは無効になるので、大きさが安定するまで取り直して待つ
- 「サインイン状態を維持」へは Tab 7 回。位置を決め打ちせず、
  **フォーカスリングを見て到達を判定**してから Space で入れる

> **「サインイン状態を維持」は自動で有効にする。** 既に有効なら触らない。
> 状態を判別できないときも触らない (誤って外さないため)。

これを無効にすると Riot はセッション cookie を保存しないので、次の機能が
**すべて使えなくなる**。毎回パスワードを打つ運用にする場合だけ切ること。

- 「現在のを取り込む」(セッションが無いので取り込めない)
- パスワード無しの切り替え
- ランク・戦績・ウォレット・所持スキンの取得 (どれも ssid cookie が要る)

切りたい場合は `%LOCALAPPDATA%\ValorantAccountManager\settings.json` に:

```json
{ "stay_signed_in": false }
```

ログイン画面は hCaptcha で保護されている。captcha や 2 段階認証が出た場合は
アプリ側では何もできないので、画面に従って対応すること。

### ショートカット

| キー | 動作 |
|---|---|
| `Ctrl+N` | アカウントを追加 |
| `Ctrl+R` | 選択中を更新 |
| `Ctrl+Shift+R` | すべて更新 |
| `F5` | 一覧を再読み込み |

## デモモード

VALORANT が入っていない PC でも、モック環境で UI と切り替えロジックを試せる。

```bash
python main.py --demo
```

偽の Riot ディレクトリ・偽のセッション YAML・偽のローカル API サーバーを立てて、
サンプルアカウント 4 件を入れた使い捨ての保管庫で起動する。実環境には一切触らない。

## 構成

```
main.py                  エントリポイント
vam/
  paths.py               Riot Client / VALORANT の場所を探す
  crypto.py              DPAPI + AES-GCM
  storage.py             暗号化された保管庫
  models.py              Account / RankInfo / WalletInfo / InventoryInfo
  service.py             中核。UI からはここだけを呼ぶ
  diagnostics.py         クラッシュログと環境情報
  riot/
    process.py           Riot 系プロセスの終了
    session.py           セッションファイルの保存・復元・解析
    launcher.py          Riot Client の起動
    localapi.py          起動中クライアントのローカル API (lockfile 経由)
    auth.py              保存 cookie からのトークン再取得 (RSO reauth)
    api.py               pd.*.a.pvp.net (ランク・ウォレット・所持品)
    content.py           valorant-api.com (名前・アイコン)
    autologin.py         パスワード自動入力 (SendInput)
  ui/                    PySide6 の画面
  mock/                  偽 Riot 環境とデモデータ
tests/test_all.py        通しテスト (VALORANT 未インストールでも全部走る)
tools/ui_shot.py         UI のスクリーンショット撮影
tools/build_exe.py       配布用 exe のビルド
```

## テスト

```bash
python tests/test_all.py
```

VALORANT が入っていなくても全項目が走る。`vam/mock/fake_riot.py` が本物と同じ
ディレクトリ構造・同じ YAML 形状・同じローカル API（自己署名 HTTPS + lockfile Basic 認証）
を用意するため、切り替えロジックは実環境と同じ経路を通る。

## セッションの有効期限

切り替えに使う Riot のセッション cookie (`ssid`) には有効期限がある。本アプリは
**再認証のたびに、Riot が返してくる新しい cookie を保管庫に書き戻す**ので、
使い続けているアカウントの期限は自動的に伸びていく。

期限が更新されるタイミング:

- 「更新」／「すべて更新」（ランク取得のたびに再認証が走る）
- 「戦績」タブの読み込み
- 右クリック →「セッションを延長」（延長だけを明示的に行う。切り替えは起きない）

残り日数は次の 3 か所に出る。

- アカウントカードの状態行（残り 7 日を切ると橙色、失効・未保存は赤）
- 概要タブ上部の帯（期限日時つき）
- 概要タブの「セッション」行と、カードのツールチップ

長く放置して期限が切れたアカウントは、Riot Client でそのアカウントにログインし直し、
右クリック →「現在のログインをこのアカウントに保存」で取り込み直す。

> 書き戻しは、読み直して**有効かつ期限が延びていること**を確認してからでないと行わない。
> 壊れた応答や期限の縮む応答で、使えているセッションを潰さないようにしてある。

## うまくいかないとき

ヘッダ右の **「?」ボタン** で診断情報が出る。Riot Client の検出結果、セッションファイルの
有無、起動中プロセスなどが並ぶので、そのままコピーして報告に使える。

予期しないエラーで落ちた場合は `%LOCALAPPDATA%\ValorantAccountManager\crash.log` に
スタックトレースが残る。

よくある詰まりどころ:

| 症状 | 原因と対処 |
|---|---|
| 「Riot Client が見つかりません」 | VALORANT 未インストール、または非標準の場所。診断情報で検出パスを確認 |
| 「ログイン中のアカウントが見つかりません」 | Riot Client のログイン時に「ログイン情報を保存する」が無効。有効にして入り直す |
| 取り込めるが「更新」が失敗する | cookie 再認証が通っていない。診断情報とエラー文言を添えて報告 |
| 切り替え後にログイン画面が出る | セッションが失効している。そのアカウントで入り直して取り込み直す |

## セッションファイルの形

切り替えの土台になるファイルなので、実機で確認した形を残しておく。
`%LOCALAPPDATA%\Riot Games\Riot Client\Data\RiotGamesPrivateSettings.yaml`

### 現行 (Riot Client v138)

**ssid cookie は存在しない。** ログイン状態は OAuth の refresh_token として持つ。

```yaml
psl:
    authorization:
        riot-client:
            id_token: "eyJraWQiOi..."          # sub=puuid, acct={game_name, tag_line}
            refresh_token: "eyJlbmMiOi..."     # JWE。中身は読めない
            last_token_creation_time: 1788488425923
            max_duration_between_restores: 3566083   # 約 41 日
            refresh_token_write_count: 3       # 使うたびに増える
riot-login:
    persist: null                              # ログイン中でも null のまま
rso-authenticator:
    tdid: { ... }                              # 端末 ID だけ
```

- **有効期限** = `last_token_creation_time / 1000 + max_duration_between_restores`
- **puuid** は `id_token` の `sub`、**Riot ID** は `acct`。
  クライアントが起動していなくても取れる
- 期限を延ばすのは Riot Client 自身の仕事。そのアカウントで一度起動すれば
  トークンが更新され、期限が先に延びる。アプリ側からの延長操作は要らない
- Riot Client を terminate しても、終了時にこのファイルは書き換えられない
  (ハッシュ・mtime とも不変)。だから終了前に退避してよい

### 旧 (ssid cookie)

`rso-authenticator` に ssid / clid / csid を持つ形。読み取りは両対応。

- cookie 名も値も引用符で囲まれる。素朴に `name:\s*(\w+)` で拾うと 1 件も取れない
- 寿命は JWT の `exp` ではなく `expiryTime`。`tdid` の JWT は
  クレームが `iat` / `id` / `nonce` だけで、`exp` も `sub` も持たない

読み取りは PyYAML。位置を決め打ちせず、`refresh_token` を持つ辞書や
`name`/`value` を持つ辞書を入れ子から拾う。書き戻しは行単位の差し替え
(YAML を書き直すと引用符や並び順が変わり、Riot Client 側が読めなくなる危険がある)。

## 情報の取得経路

ランク・ウォレット・所持品・戦績は `pd.<shard>.a.pvp.net` から引く。
アクセストークンの取り方はセッション形式で変わる。

| セッション形式 | 取得方法 |
|---|---|
| 現行 (refresh_token) | **起動中の Riot Client のローカル API から借りる。** refresh_token を消費しないので、ローテーションで元のセッションを失効させる心配がない。そのアカウントに切り替えている必要がある |
| 旧 (ssid cookie) | cookie 再認証 (RSO reauth)。切り替え不要 |

> **リージョンとシャードは別物。** ローカル API は LoL 由来のコード (`jp1` など) を
> 返すが、VALORANT のシャードは `na` / `eu` / `ap` / `kr` の 4 つしかない。
> 日本は `jp1` → `ap`。そのままホスト名に入れると `pd.jp1.a.pvp.net` となり
> 名前解決に失敗する。

## 注意## 注意

- **アカウント切り替えは Riot Client を終了させる。** ゲーム中に実行すると確認を求められる
- Riot Vanguard（カーネルドライバ）には触らない。停止すると再起動が必要になるため
- 自動入力は Riot Client の画面構成 (入力欄の位置) に依存する。Riot 側の変更で壊れうるので、通常はセッション方式を使う
- ログイン画面は hCaptcha で保護されている。本アプリは captcha に一切触れないし、サインインも押さない
- 現行形式では、ランクや所持品の取得に**起動中の Riot Client が要る**。
  他のアカウントの情報を見るには、そのアカウントに切り替えてから更新する
- 取得するのは自分のアカウントの情報のみ。他プレイヤーの情報を覗く機能は入れていない
