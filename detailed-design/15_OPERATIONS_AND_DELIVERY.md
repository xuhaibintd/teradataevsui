# 運用・依存・配布詳細設計

文書 ID：`TD-EVSUI-DD-12`

## 1. 運用モデル

アプリケーションは Python 3.11 の単一プロセスで起動し、内部 job runner を含む。SQLite、暗号鍵、uploads を永続化し、Teradata と Unstructured を外部サービスとして利用する。

## 2. 開発環境

`pyproject.toml` と `uv.lock` を依存関係の唯一の正本とする。`requirements*.txt` を追加しない。

```powershell
uv sync --locked
uv run uvicorn app.main:app --host 127.0.0.1 --port 8010
```

プロジェクト directory を移動・改名した場合、editable install と launcher の絶対 path を避けるため virtual environment を再作成し、locked sync を行う。runtime DB、credential key、uploads を削除しない。

公開する初回インストール手順は、Windows PowerShell と Linux x86-64 のそれぞれについて、Git clone、固定版 uv、Python 3.11、locked sync、初回管理者、開発起動、本番相当起動、health 確認を一続きで示す。

- `OPS-INSTALL-001`：新規利用者が空のマシンから README と初回インストールガイドだけで起動できる手順を維持する。
- `OPS-INSTALL-002`：ネイティブ起動と Docker Compose を分け、Windows と Linux の shell 差を明示する。
- `OPS-INSTALL-003`：本番例は `--reload` を使わず、`WEB_CONCURRENCY=1`、永続 DB、明示 credential key、HTTPS 境界を要求する。
- `OPS-INSTALL-004`：`.env` は Compose だけが自動読込することを明示し、ネイティブ起動で暗黙に読まれると説明しない。

## 3. 依存方針

| 区分 | 正本 |
|---|---|
| runtime direct | `project.dependencies` |
| browser test | optional `browser` |
| development | `dependency-groups.dev` |
| build | `build-system.requires` |
| transitive closure | `uv.lock` |

- Python は `>=3.11,<3.12`。
- uv は `0.12.10`。
- source build を許可せず、対応 Windows AMD64 と Linux x86-64 の wheel を固定する。
- runtime direct dependency の追加・削除・upgrade は `scripts/check_dependencies.py` の review list も更新する。
- LangChain、Torch、Transformers、Google/Vertex AI、LightGBM 等を本システムが直接使わない場合、direct dependency に追加しない。
- SDK の transitive dependency は lock で可視化し、アプリから暗黙に import しない。

## 4. Database CLI

`teradataevsui-db` は次を提供する。

| command | 動作 |
|---|---|
| `migrate` | pending migration 適用 |
| `status` | latest、applied、pending 表示 |
| `backup` | online backup と integrity check |

backup destination は source と別で未存在の path とする。定期 backup の復旧試験を行う。

## 5. Operations CLI

`teradataevsui-ops` は次を提供する。

| command | 動作 |
|---|---|
| `inventory` | filesystem と tracked artifact 集計 |
| `jobs` | recent job 表示 |
| `cleanup-artifacts` | dry-run または期限削除 |
| `enqueue-artifact-cleanup` | cleanup job 登録 |
| `run-jobs` | CLI で maintenance handler 実行 |

通常の workflow job はアプリ内 runner が処理する。別 CLI worker を常駐させない。`run-jobs` は保守 handler の明示運用に限定する。

## 6. Docker image

multi-stage build で locked production dependency を作り、runtime image へ `.venv` と `app`、LICENSE だけをコピーする。非 root user `teradataevsui`、port 8010、1 worker で実行する。

永続 volume は `data`、`uploads`、`pem_runtime` とする。Credential key は compose の必須環境変数とする。bootstrap username/password の既定は両方空としブラウザー初回設定を使用する。無人初期化の場合だけ両方を明示する。healthcheck は `/healthz` を使用する。

- `OPS-INSTALL-005`：`.env.example` と Compose に初期 username/password を埋め込まず、空の既定で `/setup` を利用可能にする。

## 7. Health と監視

`/healthz` はアプリケーション process の応答確認であり、Teradata と Unstructured の完全な業務 health を毎回実行しない。外部 service health は管理画面の明示 refresh と運用監視を分ける。

監視対象は次である。

- process と `/healthz`
- SQLite 書込み失敗、WAL/容量
- job queued/running/stale/failed 件数
- artifact 容量と期限
- Teradata 接続・health 失敗
- Unstructured submit/poll error と rate limit
- backup 成功と復旧試験日

## 8. ログ

標準出力へ構造が分かる operation、request ID、job ID、result を記録する。秘密値と文書本文を記録しない。開発 debug log を本番既定にしない。error の利用者表示と内部 trace を request ID で対応させる。

- `OPS-LOG-001`：ブラウザーが定常的に行う成功した job status poll は access log へ出力しない。認証・認可・not found・server error、および POST 等の状態変更要求は調査可能なよう保持する。

## 9. Backup と復旧

復旧単位は次である。

1. SQLite online backup
2. Credential key
3. uploads と manifest
4. application source/image version
5. 非秘密環境設定

復旧後は DB integrity、schema version、credential decrypt、artifact path、job 状態、接続 profile、Teradata remote state を確認する。stale job を即実行せず、外部副作用を照合する。

## 10. 公開・配布

公開前に個人名、IP、実 endpoint、username、token、PEM、実行報告、test screenshot、local path、DB、upload、build cache を検査する。公開文書は英語正本と日本語完全版の parity 規則に従う。本 `detailed-design/` は日本語内部規範として別管理する。

wheel は新しい build から生成し、現在 source と一致し、runtime secret と local artifact を含まないことを `verify_wheel.py` で確認する。

## 11. Upgrade

1. direct dependency の必要性と公式互換性を確認
2. version range または exact pin 更新
3. `uv lock` で全 closure を再生成
4. dependency policy の expected set をレビュー
5. production/browser exact sync
6. compile、static、unit、browser、wheel 検証
7. Teradata/Unstructured の opt-in live smoke
8. migration と rollback/restore 手順確認

SDK upgrade で応答形状が変わる場合、integration/service の adapter と契約テストを更新し、UI へ旧新両方の概念を追加して済ませない。

## 12. 検証項目

- fresh checkout から locked install と起動が成功する。
- production image が non-root、1 worker、healthcheck 付きで動く。
- volume 再作成後も永続状態が保持される。
- backup と key を使った実復旧を定期確認する。
- dependency check が余分な direct/transitive distribution を検知する。
- wheel と repository publication check が機密・個人情報を拒否する。
