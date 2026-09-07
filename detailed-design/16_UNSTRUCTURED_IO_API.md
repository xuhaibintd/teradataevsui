# Unstructured IO API 連携詳細設計

文書 ID：`TD-EVSUI-DD-16`

基準日：2026-09-07

## 1. 目的と適用範囲

本書は、共有 API 設定から Multi-Format / Multi-Format BookRAG の raw JSON 保存まで、Unstructured Platform API 連携の正規フローを一箇所に定義する。BookRAG の CSV・Teradata・Vector Store 後段は `10_DOCUMENT_PROCESSING.md` と `11_BOOKRAG.md` に従う。

対象は次の範囲である。

- System Configuration での API URL / API Key 管理
- モデルカタログと provider/model の選択
- Workflow DAG の生成と送信前検証
- on-demand job の submit、poll、診断、download
- raw JSON、manifest、ジョブ結果への引渡し
- 秘密値、失敗、再実行、試験の契約

## 2. 外部仕様と採用方式

- `UNS-SPEC-001`：Python SDK は `unstructured-client==0.46.2` に固定する。
- `UNS-SPEC-002`：本システムは本番用の Pipeline / Workflow API を使用し、legacy Partition Endpoint は使用しない。
- `UNS-SPEC-003`：現在の実行方式は、一ファイルごとに inline `job_nodes` を含む on-demand job を `POST {API_URL}/jobs/` へ送信する方式とする。
- `UNS-SPEC-004`：永続 Workflow の作成・更新・`workflow_id` 指定実行は現在の処理経路に含めない。
- `UNS-SPEC-005`：Unstructured の外部仕様と固定 SDK に差異がある場合、固定 SDK の実型と公式 REST 契約を確認し、インテグレーション境界と契約試験を同じ変更で更新する。

公式参照先：

- Workflow overview：<https://docs.unstructured.io/api-reference/workflow/overview>
- Jobs：<https://docs.unstructured.io/api-reference/workflow/jobs>
- Workflows：<https://docs.unstructured.io/api-reference/workflow/workflows>
- Available models：<https://docs.unstructured.io/api-reference/workflow/models>
- Errors：<https://docs.unstructured.io/api-reference/workflow/errors>
- Legacy Partition Endpoint：<https://docs.unstructured.io/api-reference/legacy-api/partition/overview>

## 3. 実装境界

| モジュール | 責務 |
|---|---|
| `app/routers/system_admin.py` | 管理者フォーム、保存結果、全セッション同期 |
| `app/auth_store.py` | 管理者認可、共有設定ファサード、監査 |
| `app/repositories/external_service_repository.py` | API URL と暗号化 API Key の永続化 |
| `app/services/doc_modes/ui_fields.py` | 版管理モデルカタログの読込と画面候補生成 |
| `app/services/unstructured_runtime.py` | URL、key、timeout、poll 設定の解決 |
| `app/services/unstructured_workflow_builder.py` | 画面値から Workflow DAG を生成 |
| `app/integrations/unstructured/contracts.py` | DAG と provider/model の送信前検証 |
| `app/integrations/unstructured/gateway.py` | Unstructured 境界の公開入口 |
| `app/services/unstructured_job_runner.py` | submit、429 retry、poll、診断、download |
| `app/services/workflow_jobs.py` | 実行時の最新共有設定取得と秘密値秘匿 |
| `app/services/multi_format.py` | ファイル単位実行、raw JSON、manifest、後段引渡し |

ルーターやテンプレートから Unstructured SDK を直接呼ばない。SDK/REST の名前や応答形状の差異は gateway、contract、job runner で止める。

## 4. 共有 API 設定

### 4.1 画面と保存

`System Configuration → Unstructured IO` は `API URL` と `API Key` を扱う。保存経路は `POST /admin/unstructured-config` で、enabled admin のみ実行できる。

- `UNS-CONFIG-001`：API Key は画面へ再表示しない。
- `UNS-CONFIG-002`：key 入力が空で clear 未指定の場合、既存 key を保持する。
- `UNS-CONFIG-003`：新しい key が入力された場合、既存 key を置換する。
- `UNS-CONFIG-004`：`Remove stored API key` が明示された場合、入力値より clear を優先する。
- `UNS-CONFIG-005`：API Key は CredentialVault で暗号化し、SQLite `external_service_configs.api_key_ciphertext` に保存する。URL は平文で保存する。
- `UNS-CONFIG-006`：保存後は現在保持しているブラウザーセッションへ新しい URL/key を同期し、新規または再活性化セッションも共有設定から取得する。
- `UNS-CONFIG-007`：監査ログには設定者と key の有無だけを記録し、key 本文を残さない。

画面の `Configured` は「URL と暗号化 key が保存済み」を意味する。API 到達性、key の有効性、利用枠、model 利用権限の検証結果ではない。現在、保存時の test-connection 呼出は行わない。

### 4.2 実行時の解決順序

Durable job は実行開始時に `AuthStore.get_unstructured_config()` から最新の共有 URL/key を取得し、文書処理へ実行時 override として渡す。ジョブ登録時の通常 payload へ API Key を固定しない。

`unstructured_runtime` の低優先から高優先への解決順は次のとおりである。

1. `app/config/unstructured.json`
2. `app/config/local_dev.json` の Unstructured 設定
3. job/session から渡された共有設定 override

URL が空の場合の既定値は `https://platform.unstructuredapp.io/api/v1` とする。最終的に key が空なら network I/O 前に失敗させる。

## 5. モデル管理

- `UNS-MODEL-001`：配布モデル候補の正本は `app/config/unstructured_models.example.json` とする。
- `UNS-MODEL-002`：カタログは `schema_version`、公式 source URL、`source_checked_at` を持つ。
- `UNS-MODEL-003`：非公開の `app/config/unstructured_models.json` または `UNSTRUCTURED_MODEL_CATALOG_PATH` は、機能/provider 単位で配布カタログへマージする。
- `UNS-MODEL-004`：実行時に公式文書をスクレイピングしない。Unstructured には本システムが使用できる安定したモデル一覧 API がないため、公式 Available models を確認した版管理変更で更新する。
- `UNS-MODEL-005`：model-backed enrichment で明示された provider/model は Workflow node の対応 settings へそのまま渡し、不整合は送信前に失敗させる。
- `UNS-MODEL-006`：同一 model ID が OpenAI と Azure OpenAI の両方に存在できるため、その model 名だけで明示 provider を上書きしない。
- `UNS-MODEL-007`：VLM Partitioner で provider が空なら model 名から推論できる。明白に矛盾する provider が指定された場合は警告を残して model に対応する provider へ補正するが、指定 model 自体は変更しない。

Partitioner の VLM/Auto は `settings.provider` と `settings.model` を使用する。model-backed enrichment は `settings.provider_type` と `settings.model` を使用する。`twopass_image_description` と `twopass_table2html` は platform-managed であるため provider/model を送信しない。

指定 model が実際に使用されたかの確認は、送信 DAG、job metadata、および出力に存在する場合は `metadata.enrichment_origins` を照合する。API が返さない情報を推測して成功扱いにしない。

## 6. Workflow DAG 契約

### 6.1 Partition route

| 画面 route | node type | subtype | 主な settings |
|---|---|---|---|
| Auto | `partition` | `vlm` | `is_dynamic=true` |
| VLM | `partition` | `vlm` | `is_dynamic=false` |
| Fast | `partition` | `unstructured_api` | `strategy=fast` |
| High Res | `partition` | `unstructured_api` | `strategy=hi_res` |
| OCR Only | `partition` | `unstructured_api` | `strategy=ocr_only` |

`vlm` subtype に旧 `strategy` を混在させない。partition node は厳密に一つで、DAG の先頭に置く。

### 6.2 モード別順序

```text
Multi-Format
  Partitioner
    → optional Image/Table/OCR enrichments
    → Chunker
    → optional NER

Multi-Format BookRAG
  Partitioner
    → optional Image/Table/OCR enrichments
    → optional NER
    → raw elements（Workflow Chunker なし）
```

- `UNS-DAG-001`：Fast と OCR Only は enrichment node を受け付けない。
- `UNS-DAG-002`：explicit VLM は重複する image description、table description、table-to-HTML、generative OCR を受け付けない。NER は許可する。
- `UNS-DAG-003`：model-backed prompter は subtype に対応する provider と空でない model を持つ。
- `UNS-DAG-004`：Multi-Format の非 NER enrichment は Chunker 前、NER は Chunker 後に置く。
- `UNS-DAG-005`：BookRAG は Workflow Chunker を追加せず、NER を enrichment 列の最後に置く。
- `UNS-DAG-006`：契約不整合は `validate_workflow_nodes()` で network I/O 前に失敗させる。

Multi-Format の Chunker は `chunk_by_character`、`chunk_by_title`、`chunk_by_page`、`chunk_by_similarity` をサポートする。BookRAG の section/node 構築は保存した raw elements から本システム内で行い、Unstructured の chunking と呼ばない。

## 7. On-demand job 処理順序

```text
アップロード済み文書
  → durable parse job を登録
  → 実行時に最新共有 URL/key を復号
  → 画面値を正規化して Workflow DAG を生成
  → DAG 契約をローカル検証
  → ファイルサイズを検証
  → POST /jobs/（request_data.job_nodes + input_files）
  → job ID を取得
  → jobs.get_job で状態を poll
  ├─ FAILED / STOPPED → details・failed-files を取得して失敗
  ├─ timeout → ローカル待機を失敗。remote job は取消済みとみなさない
  └─ COMPLETED
       → jobs.download_job_output
       → JSON payload から element list を抽出
       → doc_id/checksum 付き raw JSON と parse manifest を保存
       → Multi-Format または BookRAG の CSV 段階へ引渡し
```

### 7.1 Submit

- `UNS-JOB-001`：現在は一要求一ファイルで送信する。
- `UNS-JOB-002`：10,000,000 bytes を超えるファイルは送信前に拒否する。
- `UNS-JOB-003`：連続 submit は既定 1.35 秒以上空ける。
- `UNS-JOB-004`：HTTP 429 は最大 6 回まで、`Retry-After` または response の `retry_after` を尊重して再試行する。
- `UNS-JOB-005`：submit は `unstructured-api-key` header、`request_data`、`input_files` を使用し、応答に `id` がなければ失敗とする。

### 7.2 Poll、診断、download

固定 SDK 0.46.2 では次を使用する。

| 操作 | SDK method | request type |
|---|---|---|
| 状態取得 | `jobs.get_job` | `GetJobRequest` |
| 処理詳細 | `jobs.get_job_details` | `GetJobDetailsRequest` |
| 失敗ファイル | `jobs.get_job_failed_files` | `GetJobFailedFilesRequest` |
| 出力取得 | `jobs.download_job_output` | `DownloadJobOutputRequest` |

poll の既定は 2 秒間隔、最大待機 1,800 秒で、mode 別設定により上書きできる。`COMPLETED` だけを成功とし、`FAILED` と `STOPPED` は可能な範囲で詳細と失敗ファイルを取得する。

download 対象は `output_node_files` の末尾を優先し、ない場合は最初の `input_file_ids` を使う。出力は JSON 配列、または `elements`、`output`、`data` のいずれかに配列を持つ JSON object として解釈する。それ以外は未対応形状として失敗させる。

## 8. 成果物と後段

- `UNS-OUTPUT-001`：raw response は JSON-safe 化して保存し、doc_id、元ファイル名、checksum、element count、job/workflow metadata を manifest に記録する。
- `UNS-OUTPUT-002`：全ファイルが成功した parse run だけを `ready` とする。部分失敗 run は CSV 段階へ渡さない。
- `UNS-OUTPUT-003`：CSV 再生成は保存 raw JSON を使い、Unstructured API を再呼出ししない。
- `UNS-OUTPUT-004`：Multi-Format は Unstructured chunk rows、BookRAG は raw elements から document/block/node 等を生成する。
- `UNS-OUTPUT-005`：raw JSON、manifest、ジョブ結果、ログへ API Key または provider key を含めない。

## 9. 失敗と再実行

| 失敗 | 動作 |
|---|---|
| URL/key 未設定 | network 前に失敗 |
| DAG/provider/model 不整合 | network 前に失敗 |
| ファイル超過 | 対象 file result を失敗として保持 |
| 401/403 | 認証・権限失敗として安全化して返す |
| 422 | request contract 不整合として失敗 |
| 429 | 上限内で待機再試行 |
| FAILED/STOPPED | job ID と秘匿済み diagnostics を保持 |
| poll timeout | remote 状態未確定として失敗。自動取消や自動再送をしない |
| download/JSON 形状不正 | parse 失敗。CSV へ進めない |

同じ文書を再 parse する場合は新しい run と remote job を作る。成功済み raw JSON の transform/load だけをやり直す場合は既存 parse run を再利用する。

## 10. 非対象と既知の制約

- legacy Partition Endpoint
- named Workflow の CRUD と `workflow_id` 再利用
- remote job の cancel UI
- 保存画面での API 接続試験
- Unstructured からのモデル一覧自動取得
- Workflow Embedder/Destination node による Vector Store 作成

これらを追加する場合は、本書、`06_EXTERNAL_INTEGRATIONS.md`、`10_DOCUMENT_PROCESSING.md`、必要な UI 設計、契約試験を同じ変更で更新する。

## 11. 検証契約

| 対象 | 正常系 | 失敗系 |
|---|---|---|
| 共有設定 | 保存、置換、空欄維持、clear、全セッション反映 | viewer 拒否、長さ超過、復号失敗 |
| 秘密値 | 暗号化 DB、HTML 非表示、実行時復号 | response、log、job result への漏えい拒否 |
| モデル | カタログ merge、provider ごとの候補、指定値送信 | 不正 JSON、provider/model 不整合 |
| DAG | route、enrichment、chunk、NER の正式順序 | 複数 partition、順序違反、VLM 重複 enrichment |
| Job | submit、poll、diagnostics、download、element 抽出 | 429、401/403、422、FAILED、STOPPED、timeout、不正 output |
| Stage | raw JSON、checksum、ready manifest、再利用 | 部分失敗、checksum 不一致、非 ready 後段拒否 |

主な回帰入口は `tests/test_user_admin.py`、`tests/test_auth_store.py`、`tests/test_unstructured_model_catalog.py`、`tests/test_unstructured_contracts.py`、`tests/test_unstructured_job_runner.py`、`tests/test_multi_format_workflow.py`、`tests/test_workflow_jobs.py`、`tests/test_browser_actions.py` とする。実 API 検証は専用 key、利用枠、破棄可能な文書を用いる opt-in live test とし、通常テストでは外部送信しない。
