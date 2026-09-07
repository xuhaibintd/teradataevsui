# 文書処理・Multi-Format 詳細設計

文書 ID：`TD-EVSUI-DD-07`

## 1. 目的

本領域は、利用者の文書を安全に保存し、処理方式に応じて直接作成、Unstructured JSON 化、CSV 変換、Teradata table load、Vector Store 作成へ渡す。各段階を再利用可能な manifest で結び、長時間処理は永続 job として実行する。

## 2. モジュール

| モジュール | 責務 |
|---|---|
| `app/utils/uploads.py` | upload 収集、サイズ制限、安全な保存、path 解決 |
| `app/services/doc_modes/registry.py` | 3 mode handler の選択 |
| `app/services/doc_modes/constants.py` | mode 値と UI 値 |
| `app/services/doc_modes/*_mode.py` | mode ごとの preprocess と status hook |
| `app/services/multi_format.py` | parse、JSON→CSV、load、manifest、同期 pipeline |
| `app/services/multi_format_config.py` | 型、table toggle、定数 |
| `app/services/unstructured_workflow_builder.py` | Workflow node 構築 |
| `app/integrations/unstructured/` | 契約検証と gateway |
| `app/services/unstructured_job_runner.py` | submit、poll、download、diagnostics |
| `app/services/workflow_jobs.py` | durable handler |

## 3. 入力文書

各 upload に新しい UUID `doc_id` を割り当て、`uploads/documents/<doc_id>/` の管理下へ保存する。元ファイル名は metadata として保持し、path の一意性や安全性に使用しない。同一ファイルの再 upload は新しい doc_id とする。

- `DOC-UPLOAD-001`：設定上限を超えるファイルを完全保存しない。
- `DOC-UPLOAD-002`：basename 化と管理 root の解決後確認で path traversal を拒否する。
- `DOC-UPLOAD-003`：手動で解析した multipart file は成功・失敗の全経路で close する。
- `DOC-UPLOAD-004`：保存済み文書の manifest には doc_id、filename、saved_path を持たせる。
- `DOC-UPLOAD-005`：公開テスト成果物へ実文書、個人パス、内容を含めない。

## 4. 処理モード

| mode | 表示名 | 主な流れ |
|---|---|---|
| `text_core` | `Text PDF Only` | 文書 path を VectorStore.create へ渡す |
| `multi_format` | `Multi-Format` | Unstructured → JSON → 1 chunk table CSV → load → create |
| `multi_format_bookrag` | `Multi-Format BookRAG` | Unstructured → JSON → BookRAG CSV 群 → load → bnode から create |

未知 mode は互換上 `text_core` へ正規化するが、ユーザー送信時の空 mode は必須エラーとする。新しい mode は handler contract、UI field、job、manifest、test を一組で追加する。

## 5. 共通段階

```text
Upload
  → Parse run
      → document ごとの raw JSON
      → parse manifest
  → CSV run
      → checksum 検証
      → document ごとの CSV
      → CSV manifest
  → Load run
      → 接続対象検証
      → table 作成/投入
      → 行数検証
      → load summary
  → Vector Store create
```

各 run は新しいディレクトリを持ち、過去 run を上書きしない。run ID はディレクトリ名と manifest 内の ID が一致しなければならない。

## 6. Parse run

Parse は保存文書ごとに Unstructured workflow を実行する。入力は処理設定、対象文書、共有 Unstructured URL/key である。複数文書は設定上限の範囲で並行処理できるが、API submission 間隔を守る。

- `DOC-UNSTRUCTURED-001`：本システムの on-demand job 実装上限である一ファイル 10 MB（10,000,000 bytes）を送信前に検証する。これは upload 保存上限とは別の、現在の外部サービス許容量より厳格なアプリケーション制約であり、超過時は対象ファイル名を含む file result を失敗として残す。

manifest は最低限、artifact type、schema version、parse_run_id、作成時刻、Vector Store 名、全体 status、文書配列を持つ。各文書には doc_id、filename、source path、raw JSON path、checksum、element count、status、error、workflow/job metadata を持たせる。

全対象の raw JSON が有効な場合だけ run を `ready` とする。部分失敗は file result を残し、後段 CSV へ進ませない。

## 7. Unstructured Workflow

API 設定、モデル管理、node 形式、Jobs API、poll/download、失敗処理の完全な契約は `16_UNSTRUCTURED_IO_API.md` に従う。本節は文書処理から必要な DAG 条件を示す。

partition node は必ず一つで、subtype は `vlm` または `unstructured_api` とする。

- `DOC-UNSTRUCTURED-002`：次の node 形式、model 設定、DAG 順序を network I/O 前に検証する。

- `vlm` は `is_dynamic=true` を Auto、`is_dynamic=false` を explicit VLM とし、旧 `strategy` は送信しない
- `unstructured_api` は `fast`、`hi_res`、`ocr_only`
- explicit VLM へ別 image/table/OCR prompter を追加しない。NER は追加できる
- prompter settings は object とする
- image/table description、generative OCR、非 two-pass table-to-HTML、NER は `provider_type` と `model` を必須とする
- `twopass_image_description` と `twopass_table2html` は provider/model を送信しない
- image/table/OCR enrichment は chunker 前、NER は chunker 後に置く。BookRAG は chunker を持たないため NER を enrichment 列の最後に置く
- generative OCR を High Res で使う場合、`extract_image_block_types` に対象 text element type を含める
- provider、model、OCR language、chunk、table/image/NER option を型と範囲へ正規化する
- VLM と全 model-backed enrichment のモデル候補は `06_EXTERNAL_INTEGRATIONS.md` の版管理モデルカタログから取得し、UI と workflow builder にモデル一覧を重複定義しない
- model ID が OpenAI と Azure OpenAI の両方に存在する場合は、利用者が明示した provider を維持する

workflow definition は network I/O 前にローカル検証する。

## 8. Multi-Format CSV run

ready parse manifest を選び、明示的な Vector Store 名と target database を要求する。raw JSON checksum を再検証し、各文書を `UNSTRUCTURED_CHUNK_COLUMNS` 契約の CSV へ変換する。

CSV manifest は source_parse_run_id、csv_run_id、target database、table name、document outputs、件数、transform version、status、load status、Vector Store status を持つ。

CSV は document ごとの stage directory に分け、同名ファイル衝突を防ぐ。すべて成功した場合だけ `ready` とする。

文書ごとの変換完了時に durable job heartbeat を更新し、並列実行中も完了文書数に比例した 10～90 の進捗を保存する。成功・失敗のどちらの file result も完了数へ含める。

- `DOC-CSV-001`：CSV run 作成前に、両方の Multi-Format mode を横断して、同じ target database と大文字小文字を区別しない Vector Store 名を持つ `status=ready` かつ `load_status=ready` のローカル manifest を検査する。該当 run がある場合は CSV を生成せず、別名を要求する。未 load、loading、failed の run、および別 database または別名はこの検査で占有扱いにしない。Router と変換 service の両方で検査し、登録と実行の間の状態変化にも対応する。

## 9. BookRAG CSV run

BookRAG では Core、Audit、Graph の選択済み table CSV を生成する。`documents`、`blocks`、`nodes`、`document_relations` は必須 Core、`raw` は監査用、entity 系は Graph とする。契約 version と table mapping を manifest に固定する。

文書内でだけ一意な ID は必ず doc_id と組にする。document relation は全文書変換完了後に run 単位で一つ生成し、正当な関係がない場合は header-only とする。

BookRAG も各文書の Core、Audit、Graph CSV 生成が完了するたびに同じ 10～90 の進捗契約を使用する。

## 10. Load run

load 前に次を検証する。

1. CSV manifest が ready
2. artifact type と schema version
3. run ID と directory
4. target database と table mapping
5. 必須 table が有効
6. CSV path が run directory 内
7. 重複 CSV がない
8. profile ID と target fingerprint が一致

load status は `not_started → loading → ready` または `failed` とする。同じ manifest が ready の場合は保存済み summary を返せる。loading の中断を自動成功または自動再実行にしない。

投入後は table ごとの persisted row count を検証する。失敗時は安全化した error を manifest に保存する。BookRAG の失敗 cleanup は、その run が作成した対象だけに限定する。

## 11. Vector Store 作成への引渡し

Multi-Format はロード済み chunk table の `text` を data column、`id` を key として使う。BookRAG は `bnode.content` を data column、`doc_id,node_id` を複合 key とし、Description に BookRAG marker を保持する。

file-based source 用に不適切な `document_files`、ingestor、chunk parameter は create payload から除去する。loaded run の Vector Store 名と form の名前が一致しなければ拒否する。

## 12. 失敗・再利用・再実行

- parse JSON は transform algorithm を変更して CSV を再生成できる。
- CSV run は不変とし、再生成は新 run を作る。
- load 完了結果は同じ profile/target だけで再利用できる。
- failed load を再試行するときは table 状態を調査し、新 run または明示的な再実行を使う。
- Vector Store create の失敗は loaded table を自動削除しない。
- timeout 後は remote status を確認する。

## 13. 将来の分割境界

`multi_format.py` は現在、manifest、変換、load、同期互換処理を一ファイルに持つ。分割時は次の単位を推奨する。

- `manifests`：resolve、validate、list、atomic write
- `parsing`：Unstructured 実行
- `multi_format_transform`：chunk CSV
- `bookrag_transform`：BookRAG rows/CSV
- `loaders`：Teradata DDL/DML と count
- `pipeline_facade`：既存公開関数の互換入口

分割前後で manifest schema、run directory、公開関数、テスト fixture を変えない。

## 14. 検証項目

- upload size、path traversal、duplicate filename、close を試験する。
- 3 mode の必須 field と payload 除外を試験する。
- workflow node の有効・無効組合せを試験する。
- parse/CSV manifest の checksum、ID、version、部分失敗を試験する。
- target mismatch、loading recovery、ready reuse、row count 不一致を試験する。
- BookRAG 複合 key と table contract を試験する。
