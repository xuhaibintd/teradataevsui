# 構成・起動詳細設計

文書 ID：`TD-EVSUI-DD-02`

## 1. 責務

`app/core/settings.py` は実行時設定の唯一の型付き入口である。`app/main.py` は設定を受け取ってアプリケーションを組み立て、`app/web_support.py` はアプリケーション状態と永続サービスを初期化する。設定不正は外部接続を開始する前に起動失敗として扱う。

## 2. 設定ソースと優先順位

| 優先 | ソース | 用途 |
|---:|---|---|
| 1 | 明示的に渡した `Settings` | テスト、埋め込み起動 |
| 2 | 環境変数 | 本番と標準開発 |
| 3 | 安全な既定値 | 非シークレット設定 |
| 4 | `local_dev.json` | 旧ローカル開発互換と初回 bootstrap |
| 5 | SQLite | 起動後の共有接続、外部サービス、ユーザー |

`local_dev.json` は継続的な本番設定の正本にしない。SQLite に移行された共有設定を優先し、シークレットをソース管理へ入れない。

## 3. 環境変数契約

| 変数 | 既定 | 制約・意味 |
|---|---|---|
| `EVSUI_ENVIRONMENT` | `development` | `development`、`test`、`production` |
| `WEB_CONCURRENCY` | `1` | 必ず `1` |
| `EVSUI_DATABASE_PATH` | `data/evsui.db` | SQLite 正本 |
| `EVSUI_CREDENTIAL_KEY` | 空 | Fernet キー。設定時はファイルより優先 |
| `EVSUI_CREDENTIAL_KEY_FILE` | DB 隣接 | キーファイルの明示位置 |
| `EVSUI_BOOTSTRAP_ADMIN` | 空 | 無人初期化用の初回管理者。password と同時指定 |
| `EVSUI_BOOTSTRAP_PASSWORD` | 空 | 無人初期化用。8 文字以上 |
| `EVSUI_SESSION_TTL_SECONDS` | `28800` | 300 秒以上 |
| `EVSUI_EXTERNAL_API_ENABLED` | token 有無 | 外部 BookRAG API の有効化 |
| `EVSUI_API_TOKEN` | 空 | 外部 API 有効時に必須 |
| `EVSUI_CSRF_ENABLED` | `true` | ブラウザー変更要求の同一生成元検証 |
| `EVSUI_MAX_UPLOAD_BYTES` | `104857600` | 1 アップロード上限 |
| `EVSUI_ARTIFACT_RETENTION_DAYS` | `30` | 成果物の標準保存日数 |
| `EVSUI_ARTIFACT_CLEANUP_ENABLED` | `false` | 実削除を許可する明示スイッチ |
| `EVSUI_JOB_STALE_SECONDS` | `900` | stale 判定。60 秒以上 |
| `EVS_VECTORSTORE_READY_TIMEOUT_SECONDS` | `7200` | Ready 待機上限。60 秒以上 |
| `EVS_VECTORSTORE_READY_POLL_SECONDS` | `5` | 状態確認間隔。0.1 秒以上 |

文書処理固有の環境変数は `unstructured_runtime.py` で境界値へ正規化する。新しい環境変数は型、既定値、最小・最大、シークレット性、適用タイミングを定義する。

## 4. 本番起動条件

- `EVSUI_ENVIRONMENT=production`
- `WEB_CONCURRENCY=1`
- 事前作成した `EVSUI_CREDENTIAL_KEY` または明示的な key file
- 初回ブラウザー設定、または初回だけ有効な管理者 bootstrap 情報
- 書込み可能で永続化された `data/` と `uploads/`
- SDK に必要な場合、保護された `pem_runtime/`
- TLS 終端と安全な外部公開設定

本番では CredentialVault がキーを自動生成しない。外部 API は既定で無効とする。

## 5. 初期化シーケンス

```text
Settings.from_env
  → validate_runtime
  → FastAPI / middleware / routers
  → initialize_app_state
      → AuthStore.initialize → migrations
      → credential key 検証
      → bootstrap user/config
      → legacy PEM/profile migration
      → repositories / artifact lifecycle
  → lifespan
      → SingleInstanceLock
      → job recovery
      → ApplicationJobRunner.start
```

- `START-001`：設定検証より前にネットワーク接続しない。
- `START-002`：マイグレーションは冪等であること。
- `START-003`：bootstrap は既存ユーザー、保存済みシークレット、接続プロファイルを上書きしない。
- `START-004`：テスト環境ではバックグラウンド runner を自動開始しない。
- `START-005`：アプリ起動失敗を握りつぶして部分稼働させない。
- `START-006`：bootstrap 後にユーザーが 0 件なら、ログイン画面ではなく一回限りの初回管理者設定へ誘導する。
- `START-007`：ユーザーが 1 件以上存在する場合、初回設定入口を無効化してログインへ戻す。
- `START-008`：初回管理者作成は一つの SQLite write transaction で 0 件確認と insert を行い、同時要求から複数の初回管理者を作らない。
- `START-009`：`.env.example` と Compose の既定では bootstrap username/password を空にし、対話型初回設定を妨げない。無人初期化では両方を同時に明示する。

## 6. パス設計

| パス | 内容 | ソース管理 |
|---|---|---|
| `app/templates/` | Jinja2 | 対象 |
| `app/static/` | CSS、JS、画像 | 対象 |
| `app/config/*.example.json` | 匿名例 | 対象 |
| `app/config/bookrag_*.json` | バージョン管理対象規則 | 対象 |
| `data/evsui.db` | 環境固有 DB | 対象外 |
| `data/*.credentials.key` | 暗号鍵 | 対象外 |
| `uploads/documents/` | 入力文書 | 対象外 |
| `uploads/multi_format_stage/` | JSON、CSV、manifest | 対象外 |
| `pem_runtime/` | 復号済み PEM | 対象外 |

モジュール import 時のディレクトリ作成は現在の互換動作である。新しい保存領域は Settings または lifecycle 初期化時に明示的に作成する。

## 7. 起動と停止

開発時の標準起動は一つの Uvicorn プロセスとする。

```powershell
uv sync --locked
uv run uvicorn app.main:app --host 127.0.0.1 --port 8010
```

停止は Uvicorn プロセスへ通常の終了シグナルを送り、lifespan の終了処理を通す。プロセスを強制終了した場合、実行中ジョブは heartbeat の stale 判定後に回収対象となるが、外部操作の停止は保証されない。

## 8. 構成変更規則

- 再起動が必要な設定と System Configuration で即時反映する設定を区別する。
- 環境変数名の互換性を不用意に破壊しない。
- プロジェクト名変更のために `EVSUI_*`、DB 名、cookie 名を一括変更しない。
- 設定値をログ出力するときはシークレットを除外する。
- Boolean の未知値を黙って false にせず起動エラーにする。

## 9. 検証項目

- 開発、テスト、本番の各設定で期待する既定と拒否条件を確認する。
- 外部 API 有効かつ token 空、本番 key 不在、複数 Worker を起動前に拒否する。
- DB、uploads、PEM のパスが移動後のプロジェクト位置を正しく参照する。
- 通常停止で runner が終了し、DB が整合している。
- `.env.example`、compose、Settings の変数が一致する。
