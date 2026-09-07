# 外部インテグレーション詳細設計

文書 ID：`TD-EVSUI-DD-11`

## 1. 方針

外部製品の SDK、応答形状、認証、timeout、rate limit、例外をアプリケーション中心部へ直接広げない。インテグレーション境界で検証・正規化し、業務サービスには安定した Python data または context manager を渡す。

## 2. Teradata

### 2.1 依存

- `teradatagenai==20.0.0.9`
- `teradataml>=20.0.0.11,<20.1`

`app/teradata_runtime.py` が `VSManager`、`VectorStore`、必要な collection 関連 symbol、`set_auth_token`、`create_context`、`remove_context`、`execute_sql` を一箇所で import する。import 失敗は診断文字列として保持し、利用時に明確な unavailable を返す。

### 2.2 呼出境界

- context/auth：`integrations/teradata/connection.py`
- Vector Store 管理：`services/vector_management.py`
- create/search/destroy：workflows と job handlers
- BookRAG SQL：`services/teradata_sql.py` と BookRAG services

SDK object を session、SQLite、job payload、JSON response へ保存しない。呼出結果は dict/list/string/table へ変換する。

### 2.3 互換性

固定 version の正式 API を基準にする。応答の alias 正規化は一箇所に集約する。古い世代との互換コードは既存データ移行に必要な場合だけ維持し、UI に V1/V2 を並べない。

## 3. Unstructured Platform

共有設定から on-demand job、モデル、DAG、成果物までの正規フローは `16_UNSTRUCTURED_IO_API.md` を正本とする。本節は外部境界の要約を示す。

### 3.1 構成

共有 `API URL` と暗号化 `API Key` を SQLite に保存する。job 実行時にだけ復号する。model/provider ごとの追加 secret は通常 payload と分離する。

- `UNSTRUCTURED-API-001`：SDK は `unstructured-client==0.46.2` に固定し、Jobs API の `get_job_details`、`get_job_failed_files`、`download_job_output` をその版の型で使用する。

### 3.2 Workflow contract

`validate_workflow_nodes()` は network 前に次を検証する。

- node が一つ以上
- partition node が厳密に一つで先頭にあること
- `vlm` subtype の Auto/VLM を boolean `is_dynamic` で区別し、旧 `strategy` を混在させないこと
- `unstructured_api` subtype と `fast`、`hi_res`、`ocr_only` strategy の組合せ
- settings が object
- explicit VLM partition と別 image/table/OCR enrichment の禁止。NER は許可する
- model-backed prompter の `provider_type` と `model`、subtype/provider 整合性
- two-pass prompter が `provider_type` と `model` を送信しないこと
- enrichment は chunk 前、NER は chunk 後という DAG 順序

`unstructured_workflow_builder.py` は UI 値から Pipeline API request を組み立てる。provider 推論、model、chunk、OCR、table/image/NER の値を正規化し、未対応 node を生成しない。

- `UNSTRUCTURED-MODEL-001`：`app/config/unstructured_models.example.json` を配布時モデルカタログの正本とし、公式モデル一覧 URL、schema version、確認日を保持する。
- `UNSTRUCTURED-MODEL-002`：`app/config/unstructured_models.json` または `UNSTRUCTURED_MODEL_CATALOG_PATH` の非公開上書きは、機能および provider 単位で配布カタログへマージする。不正 JSON または存在しないファイルは配布カタログを破壊しない。
- `UNSTRUCTURED-MODEL-003`：公開モデル一覧 API がない間は、アプリ起動時に文書ページをスクレイピングしない。公式文書を確認した版管理変更と契約試験で更新する。
- `UNSTRUCTURED-MODEL-004`：同じ model ID を複数 provider が使用できる場合、明示された provider を model 名の接頭辞推論で上書きしない。
- `UNSTRUCTURED-API-002`：利用者が指定した enrichment model は `settings.model`、provider は `settings.provider_type` として変更せず送信する。不整合は network 前に失敗させ、別 model へ黙って置換しない。

### 3.3 Job protocol

```text
client create
  → on-demand job submit
  → job ID
  → poll status
  → failure diagnostics
  → output download
  → element list extraction
```

Unstructured の公式仕様（2026-09-07 確認）では、一つのローカルファイル処理 job に最大 10 ファイル、各 50 MB まで送信でき、submit 間隔は 1 秒以上、同時実行は最大 5 job とされる。本システムはより厳格な実装契約として、一要求一ファイル、10 MB（10,000,000 bytes）以下、既定 1.35 秒以上の submit 間隔を採用する。rate limit 応答の retry-after を尊重する。poll timeout は job のリモート取消を意味しない。diagnostics は秘密を除いて保存する。

### 3.4 応答保存

raw response は JSON-safe 化し、doc_id と checksum を付けた stage file として保存する。CSV transform は保存 JSON を正本として再利用し、再度 Unstructured を呼ばない。

## 4. ファイルシステム

### 4.1 保存領域

| 領域 | 内容 | 管理者 |
|---|---|---|
| `uploads/documents` | 原文書 | upload service + artifact lifecycle |
| `uploads/multi_format_stage` | raw JSON、CSV、manifest | multi_format service |
| `pem_runtime` | SDK 用復号 PEM | CredentialVault |
| `data` | SQLite、key、backup | DB/operations |

path は resolve 後に許可 root 配下であることを検証する。相対 path の探索先を増やす場合は優先順位を設計し、同名 file の曖昧解決を避ける。

### 4.2 Atomic write

manifest は一時 file へ完全出力してから同一 file system 上で置換する。running、ready、failed の更新途中に破損 JSON を残さない。CSV と raw JSON の checksum を manifest に記録し、後段で再検証する。

## 5. HTTP とブラウザーライブラリ

- FastAPI/Starlette：routing、middleware、session cookie 応答
- Jinja2：server rendering
- HTMX：部分更新と job polling
- httpx / unstructured-client：外部 HTTP
- Playwright：browser test の optional dependency

外部 CDN は現在 HTMX のみ許可されている。CSP と offline 要件を変更する場合は配信方法を再評価する。inline style/script を増やさず、CSP を弱めない。

## 6. Timeout と retry

| 対象 | Retry 方針 |
|---|---|
| Teradata health/list/status | 読み取りとして制限付き |
| Teradata create/destroy/load | 自動再試行しない |
| Unstructured submit | retry-after と安全条件に従う |
| Unstructured poll | timeout まで反復 |
| output download | job 成功確認後に制限付き |
| local file write | 原子置換。容量/権限修正後に再実行 |

retry 回数、backoff、timeout は隠れた無限 loop にせず構成と診断へ反映する。

## 7. 観測性

外部呼出のログはサービス名、operation、request/job ID、対象の非秘密識別子、経過、結果を含める。password、token、API key、PEM、document content、Authorization header は含めない。

## 8. 検証項目

- SDK import 不可、認証失敗、応答 alias、timeout を試験する。
- Unstructured node contract、429、failed job、poll timeout、download 不正形状を試験する。
- path traversal、root 外 artifact、manifest 破損、checksum 不一致を試験する。
- secret が mock call trace、exception、log、stage JSON に残らない。
- lockfile にない package へ暗黙依存しない。
