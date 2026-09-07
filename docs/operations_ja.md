# teradataevsui 運用ガイド

> **言語:** [English](operations.md) | 日本語
<!-- Source-SHA256: c20a33740227ab01ae80a30458eb7037415893626a0882a26c370e0d2a12d56c -->

Windows、Linux、または Docker Compose への新規デプロイでは、[初回インストールガイド](installation_ja.md) から始めてください。本書はインストール後の継続運用と復旧を扱います。

## ローカル開発

teradataevsui は、プロジェクト固有のライフサイクルラッパーや個別に管理するジョブサービスを使用せず、標準的な単一の Python プロセスで動作します。

```powershell
uv sync --locked
uv run --locked --no-sync python -m app.db migrate
uv run --locked --no-sync python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8010
```

アプリケーションはバックグラウンドジョブランナーを自動的に起動します。ドキュメント解析、CSV の生成／ロード、および Vector Store の作成は、HTTP イベントループをブロックせずに一度に 1 件ずつ実行されます。`Ctrl+C` でアプリケーションを停止できます。シャットダウン時は現在のジョブの完了を待機し、新たなジョブは取得しません。データベース単位のロックにより、同じ SQLite データベースを使用する 2 つ目のアプリケーションプロセスは拒否されます。

依存関係ポリシー、ブラウザー操作、パッケージ内容の検査を含む CI 相当の完全な検証については、[testing_ja.md](testing_ja.md) を参照してください。バックエンドの簡易検査は次のとおりです。

```powershell
uv run --locked --no-sync ruff check app tests scripts
uv run --locked --no-sync python -m compileall -q app scripts
uv run --locked --no-sync python -m unittest discover -s tests -q
```

## データベースのライフサイクル

`data/evsui.db` はランタイム状態であり、意図的に Git の追跡対象外にしています。新しいチェックアウトでは、連番付きマイグレーションからデータベースが作成されます。既存環境は起動時に冪等にアップグレードされるほか、次のコマンドで明示的にアップグレードできます。

```powershell
.\.venv\Scripts\python.exe -m app.db status
.\.venv\Scripts\python.exe -m app.db migrate
```

現在のスキーマバージョン 9 には、次のテーブルが含まれます。

| テーブル | 用途 |
|---|---|
| `schema_versions` | 適用済みマイグレーションの履歴 |
| `users`, `sessions`, `permissions`, `audit_logs` | ID 情報、アクセス権、不透明なサーバーセッション、監査イベント |
| `system_connection_profiles` | 暗号化されたパスワード、PAT、PEM を持つ再利用可能な Teradata プロファイル |
| `external_service_configs` | 共有サービスのエンドポイントと暗号化された API キー（現在は Unstructured IO） |
| `jobs` | キュー待ち／実行中／完了の永続状態、進捗、一時的に暗号化されたジョブシークレット |
| `artifacts` | 追跡対象ファイルのメタデータと保持状態 |
| `connection_configs`, `system_connection_config` | 旧リリースからの移行用に維持される互換性テーブル |

稼働中の SQLite データベースは、オンラインバックアップ API を使用してバックアップしてください。

```powershell
.\.venv\Scripts\python.exe -m app.db backup
```

既定の保存先は `data/backups/` です。認証情報キーもデータベースと一緒にバックアップしてください。同じ Fernet キーがなければ、暗号化されたデータベースパスワード、PAT、PEM の内容、および外部 API キーを復元できません。

## アーティファクトと永続メンテナンスジョブ

インベントリは読み取り専用です。クリーンアップも、環境フラグと `--apply` の両方が指定されない限りドライランになります。

```powershell
.\.venv\Scripts\python.exe -m app.ops inventory
.\.venv\Scripts\python.exe -m app.ops cleanup-artifacts
$env:EVSUI_ARTIFACT_CLEANUP_ENABLED = "true"
.\.venv\Scripts\python.exe -m app.ops cleanup-artifacts --apply
```

Teradata SDK のコンテキストはプロセス全体で共有されるため、バックグラウンドランナーは一度に 1 件のジョブを実行します。SDK 呼び出しによってブロックされている間も、30 秒ごとにハートビートを記録します。起動時に、ハートビートが `EVSUI_JOB_STALE_SECONDS` より古いジョブはキューに戻されます。キュー待ちのジョブは UI からキャンセルできますが、すでに実行中の外部操作を強制終了することはありません。

Vector Store の準備完了状態は、`EVS_VECTORSTORE_READY_POLL_SECONDS`（既定値 5 秒）ごとに、最大 `EVS_VECTORSTORE_READY_TIMEOUT_SECONDS`（既定値 7200 秒）までポーリングされます。タイムアウトするとジョブは失敗として記録されますが、Teradata がすでに受け付けたリモート処理はキャンセルされません。再試行する前にリモートの状態を確認してください。

対象となるのは、`uploads/` 以下にある、追跡済みかつ期限切れのファイルだけです。既存の未追跡ファイルが自動的に登録または削除されることはありません。

## 本番環境の要件

- `EVSUI_ENVIRONMENT=production` を設定してください。
- `WEB_CONCURRENCY=1` を設定してください。
- `EVSUI_CREDENTIAL_KEY`、または明示的に事前作成した `EVSUI_CREDENTIAL_KEY_FILE` を指定してください。
- `data/`、`uploads/`、`pem_runtime/` 用の永続ストレージをマウントしてください。
- 信頼できるリバースプロキシで HTTPS を終端し、`Host`、`Origin`、`X-Forwarded-Proto` を正しく維持してください。
- 外部 API は必要な場合にのみ、`EVSUI_EXTERNAL_API_ENABLED=true` と強力な `EVSUI_API_TOKEN` を設定して有効化してください。
- オンラインデータベースバックアップをスケジュールし、クリーンアップを有効にする前にアーティファクトのドライランインベントリを実行してください。

`compose.yaml` は 1 つのアプリケーションサービスを起動します。その HTTP サーバーとバックグラウンドジョブランナーは、同じ SQLite リポジトリ、認証情報ボールト、アーティファクトライフサイクル、および Teradata ランタイムロックを共有します。Compose の既定では二つの bootstrap 変数を空にし、新規デプロイで一回限りのブラウザー設定を開きます。無人初期化の場合だけ、両方の変数を明示的に設定します。
