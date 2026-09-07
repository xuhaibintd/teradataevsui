# モジュール責務・開発境界詳細設計

文書 ID：`TD-EVSUI-DD-MOD`

対象：Python 実装、エントリーポイント、保守スクリプト、対応テスト
関連文書：`01_SYSTEM_ARCHITECTURE.md`、`14_TEST_AND_QUALITY.md`

## 1. 目的

本書は、ソースファイルを機能単位へ対応付け、どの変更をどのモジュールへ置くべきかを定義する。単なるファイル一覧ではなく、責務、許可される依存先、禁止事項、主要な試験入口を示す。

新機能を追加するときは、既存の責務へ収まるかを先に判断する。収まらない場合は、巨大な既存ファイルへ追記せず、新しいサービスまたはワークフロー境界を設計する。

## 2. 依存方向

```text
HTTP / CLI エントリーポイント
        ↓
ルーター / コマンド調停
        ↓
ワークフロー / アプリケーションサービス
        ↓
ドメインサービス / リポジトリ
        ↓
インテグレーション / SQLite / Teradata / ファイルシステム
```

- `DD-MOD-001`：下位層はルーター、テンプレート、ブラウザー状態へ依存しない。
- `DD-MOD-002`：ルーターから外部 SDK を直接操作しない。
- `DD-MOD-003`：サービスへ `Request`、HTML、Jinja2 テンプレートを渡さない。
- `DD-MOD-004`：リポジトリは業務画面の文言や HTTP 応答を生成しない。
- `DD-MOD-005`：外部 SDK 固有例外は境界で正規化し、シークレットを除去して上位へ返す。
- `DD-MOD-006`：モジュール間で共有する値は、暗黙の可変グローバルではなく引数、戻り値、型付き設定、または明示した状態所有者を使う。

## 3. アプリケーションルート

| モジュール | 責務 | 許可される主な依存先 | 禁止事項 | 主な試験 |
|---|---|---|---|---|
| `app/main.py` | アプリケーションファクトリー、ミドルウェア、ルーター、ライフスパン、アプリ内ジョブ実行器の組立て | `core`、ルーター、ジョブサービス | 業務 SQL、画面固有処理、SDK 操作 | `test_settings.py`、`test_security.py`、`test_background_jobs.py` |
| `app/runtime.py` | アプリケーションが共有する実行時依存と初期化済みオブジェクトの組立て | Settings、AuthStore、テンプレート、Teradata 実行時 | リクエスト単位の画面判断 | `test_runtime_manager.py`、`test_local_config.py` |
| `app/teradata_runtime.py` | Teradata SDK の認証、接続、切断、現在コンテキスト操作を集約 | `teradatagenai`、`teradataml` | HTML、SQLite ユーザー管理 | `test_runtime_manager.py`、`test_live_smoke.py` |
| `app/session_state.py` | ログインセッションに対応する画面状態の生成、活性化、保存 | AuthStore、Starlette セッション | 永続業務データの正本化、SDK 長時間処理 | `test_connect_panel.py`、`test_action_routes.py` |
| `app/web_support.py` | 画面構築に必要な表示用コンテキスト、Vector Store 応答の表示正規化 | サービス、テンプレート向けデータ | 接続プロファイル永続化、秘密値の出力 | `test_connect_panel.py`、`test_frontend_parameters.py` |
| `app/auth_store.py` | 認証・設定用ファサード。各リポジトリと暗号化処理を既存呼び出し側へ提供 | `repositories`、SQLite、CredentialVault | 新しい巨大 SQL 群の追加、画面描画 | `test_auth_store.py`、`test_user_admin.py` |
| `app/local_config.py` | 開発用ローカル設定の読み込みと既定値変換 | ローカル設定ファイル | 本番シークレットの正本化、応答への値展開 | `test_local_config.py` |
| `app/catalog.py` | SDK 機能カタログの静的定義と検索 | Python introspection、SDK import | 業務機能の有効性判定 | 現在は直接試験なし |

`app/catalog.py` は現行ルーター、ワークフロー、サービスから参照されない。公開機能ではなく、削除候補として扱う。削除時はリポジトリ全体の参照、文書、パッケージ成果物を確認する。

## 4. コア基盤

| モジュール | 責務 | 変更時の契約 | 主な試験 |
|---|---|---|---|
| `app/core/settings.py` | 環境変数、既定値、型変換、起動前検証 | 設定名、型、既定値、必須条件を `03_CONFIGURATION_AND_STARTUP.md` と同期する | `test_settings.py` |
| `app/core/security.py` | CSRF、同一生成元判定、セキュリティヘッダー、秘密値の再帰的秘匿 | 失敗時にも秘密値を返さず、安全な GET を破壊しない | `test_security.py` |
| `app/core/errors.py` | 共通 HTTP 例外処理と要求 ID の関連付け | 本番応答へスタックトレースを出さない | `test_security.py`、ルーターテスト |
| `app/core/access_logging.py` | Uvicorn access log の低価値な成功 job poll を除外 | 失敗応答と状態変更要求の access log を保持する | `test_access_logging.py` |
| `app/core/runtime_manager.py` | プロセス共有 Teradata コンテキストの排他制御と要求境界の後始末 | 共有 SDK 状態を同時要求間で混線させない | `test_runtime_manager.py` |
| `app/core/single_instance.py` | 同一制御 DB に対する単一プロセスロック | ロック取得失敗を明示し、多重起動を許容しない | 起動試験 |
| `app/core/form_fields.py` | 作成フォーム文字列の上限取得と検証 | ルーターごとに異なる上限ロジックを作らない | `test_create_field_limits.py` |

## 5. SQLite とリポジトリ

### 5.1 データベース基盤

| モジュール | 責務 | 禁止事項 | 主な試験 |
|---|---|---|---|
| `app/db/sqlite.py` | 接続生成、行形式、トランザクション境界の基礎 | 業務テーブル固有ロジック | `test_migrations.py` |
| `app/db/migrations.py` | バージョン順のスキーマ作成・移行・状態確認 | 適用済み移行の意味変更、起動時の破壊的変更 | `test_migrations.py` |
| `app/db/backup.py` | SQLite オンラインバックアップ | ファイル単純コピーによる不整合バックアップ | 運用試験 |
| `app/db/__main__.py` | migrate、status、backup の CLI | Web アプリ起動、秘密値表示 | CLI 試験 |

### 5.2 リポジトリ

| モジュール | 所有データ | 公開責務 | 主な試験 |
|---|---|---|---|
| `app/repositories/user_repository.py` | ユーザー | 作成、検索、ロール、無効化、認証失敗状態 | `test_auth_store.py`、`test_user_admin.py` |
| `app/repositories/session_repository.py` | サーバー側セッション | ハッシュ化 ID による作成、取得、更新、失効、期限切れ削除 | `test_auth_store.py` |
| `app/repositories/external_service_repository.py` | 外部サービス設定 | サービス別設定の取得・保存・削除 | `test_auth_store.py` |
| `app/repositories/job_repository.py` | 永続ジョブ | 登録、取得、claim、heartbeat、完了、失敗、中断、復旧 | `test_jobs_and_artifacts.py`、`test_background_jobs.py` |
| `app/repositories/artifact_repository.py` | 成果物台帳 | 登録、所有者・ジョブ検索、保持期限、削除状態 | `test_jobs_and_artifacts.py`、`test_upload_lifecycle.py` |

- `DD-MOD-DB-001`：SQL の追加先は、所有テーブルに対応するリポジトリとする。
- `DD-MOD-DB-002`：複数リポジトリをまたぐ業務処理は AuthStore またはサービスで調停する。
- `DD-MOD-DB-003`：スキーマ変更はマイグレーションと回帰試験を同時に追加する。

## 6. HTTP ルーター

| モジュール | 経路群 | 責務 | サービスへの委譲先 | 主な試験 |
|---|---|---|---|---|
| `app/routers/auth.py` | 初回管理者設定、ログイン、ログアウト | 初期化状態、入力、認証結果、Cookie 遷移 | AuthStore、SessionRepository | `test_auth_store.py`、`test_user_admin.py`、ブラウザー試験 |
| `app/routers/system_admin.py` | System Configuration、接続プロファイル、Unstructured、ユーザー管理 | 管理者認可、フォーム検証、PRG 応答 | AuthStore、CredentialVault | `test_user_admin.py`、`test_browser_actions.py` |
| `app/routers/web.py` | 接続、管理、作成、検索、BookRAG 統制、JSON Inspector | Web 操作の受付、サービス調停、部分 HTML 応答 | workflows、vector_management、BookRAG サービス | `test_action_routes.py`、`test_browser_actions.py`、各機能試験 |
| `app/routers/jobs.py` | ジョブ登録、状態取得、中断 | 所有者認可、秘密 payload 分離、進行表示 | JobRepository、ApplicationJobRunner | `test_background_jobs.py`、`test_jobs_and_artifacts.py` |
| `app/routers/api.py` | `/api/bookrag/*`、`/healthz` | API 認証、Pydantic 契約、状態コード、メタデータ | BookRAG retrieval、Teradata runtime | `test_api_router.py`、`test_bookrag_retrieval.py` |

- `DD-MOD-HTTP-001`：ルーター関数内の計算が再利用可能、外部 I/O を伴う、または複数経路で共有される場合はサービスへ移す。
- `DD-MOD-HTTP-002`：POST 成功後は、重複実行を避けるため PRG または明示した HTMX 更新契約を使う。
- `DD-MOD-HTTP-003`：画面メッセージは `13_UI_DESIGN.md` の唯一のトップメッセージ領域へ集約する。
- `DD-MOD-HTTP-004`：入力エラー、権限エラー、競合、外部障害を同じ 500 応答へ潰さない。

## 7. ワークフロー

| モジュール | 開始条件 | 責務 | 完了条件 | 主な試験 |
|---|---|---|---|---|
| `app/workflows/create_flow.py` | 検証済み作成入力と選択モード | モード委譲、作成 payload、VectorStore.create、状態遷移 | 作成受付または明確な失敗 | `test_create_flow.py`、`test_durable_workflow_regressions.py` |
| `app/workflows/create_status.py` | 作成済みまたは作成中の対象 | status 取得、READY 判定、表示用正規化 | 最新状態を返す | `test_create_flow.py` |
| `app/workflows/destroy_flow.py` | 明示選択と削除確認 | 対象固定、destroy、409 等の正規化、一覧更新 | 削除済みまたは再試行可能状態 | `test_destroy_flow.py` |
| `app/workflows/chat_flow.py` | READY な Vector Store と質問 | similarity search、ask、回答状態の組立て | 回答または秘匿済み失敗 | 検索・アクション試験 |

ワークフローは「一連のユースケース」を所有する。単一の純粋変換はサービスへ、HTTP 詳細はルーターへ、外部製品の接続詳細はインテグレーションへ置く。

## 8. 外部インテグレーション

| モジュール | 外部境界 | 責務 | 主な試験 |
|---|---|---|---|
| `app/integrations/teradata/connection.py` | Teradata / UES | 保存済みプロファイルの一時活性化と確実な後始末 | `test_runtime_manager.py`、ライブ試験 |
| `app/integrations/unstructured/contracts.py` | Unstructured workflow schema | 送信前ノード契約の検証 | `test_unstructured_contracts.py` |
| `app/integrations/unstructured/gateway.py` | Unstructured API | クライアント作成、送信間隔、ファイル単位実行の薄い境界 | `test_unstructured_job_runner.py` |

外部ライブラリのバージョン差異はこの層または専用サービスで吸収する。ルーターとテンプレートへ SDK の生データ構造を漏らさない。

## 9. 共通業務サービス

| モジュール | 責務 | 主な呼び出し元 | 主な試験 |
|---|---|---|---|
| `app/services/credential_vault.py` | Fernet による暗号化・復号、暗号鍵検証 | AuthStore、ジョブ payload | `test_auth_store.py`、`test_security.py` |
| `app/services/vector_management.py` | ヘルス、一覧、説明取得、選択、詳細、セッション操作の正規化 | Web ルーター | `test_vector_management.py` |
| `app/services/teradata_sql.py` | 識別子、リテラル、型、テーブル確認、行数確認の安全な SQL 補助 | 文書ロード、BookRAG | 各ロード・BookRAG 試験 |
| `app/services/artifact_lifecycle.py` | 成果物登録、期限判定、安全な削除 | Web、ジョブ、保守 | `test_upload_lifecycle.py`、`test_jobs_and_artifacts.py` |
| `app/services/job_worker.py` | 永続ジョブの claim、実行、heartbeat、結果確定 | ApplicationJobRunner | `test_background_jobs.py` |
| `app/services/workflow_jobs.py` | 文書処理・作成ジョブ種別と handler の組立て | main、jobs router | `test_workflow_jobs.py`、`test_durable_workflow_regressions.py` |
| `app/services/maintenance_jobs.py` | `artifact.cleanup` 等の保守 handler | main、ops | `test_jobs_and_artifacts.py` |
| `app/services/unstructured_job_runner.py` | On-demand job の作成、待機、診断、出力取得 | multi-format 処理 | `test_unstructured_job_runner.py` |
| `app/services/unstructured_workflow_builder.py` | モード別 workflow 定義の構築 | multi-format 処理 | `test_unstructured_contracts.py`、`test_multi_format_workflow.py` |
| `app/services/unstructured_runtime.py` | API URL、timeout、poll、言語、strategy の設定解決 | Unstructured サービス | `test_unstructured_job_runner.py` |
| `app/services/unstructured_json_inspector.py` | 登録済み JSON の安全な列挙・解決・表示データ化 | Web ルーター | `test_json_inspector.py` |
| `app/services/create_config.py` | Vector Store 作成入力の設定定義と変換 | create flow、UI fields | `test_create_flow.py`、`test_frontend_parameters.py` |
| `app/services/multi_format_config.py` | Multi-Format 設定の正規化、既定値、不要 create 引数除去 | multi-format | `test_multi_format_workflow.py` |
| `app/services/multi_format.py` | Unstructured→JSON→CSV→Teradata→BookRAG の現行処理本体 | doc mode、workflow jobs | `test_multi_format_workflow.py`、BookRAG 各試験 |

`app/services/multi_format.py` は複数段階を含む大規模モジュールである。機能追加を継続して集約してはならない。段階的に次の境界へ移す。

1. マニフェストとステージディレクトリ
2. Unstructured parse
3. JSON 正規化と CSV 生成
4. Teradata table load
5. BookRAG table materialization
6. 実行結果と計測

移動は公開関数契約と既存マニフェスト互換性を保った小単位で行う。

## 10. 文書処理モード

| モジュール | 役割 | 状態 |
|---|---|---|
| `app/services/doc_modes/registry.py` | モード名から正式 handler を解決 | 正式入口 |
| `app/services/doc_modes/text_core.py` | Text PDF Only の前処理 | 正式実装 |
| `app/services/doc_modes/multi_format_mode.py` | Multi-Format の段階処理、作成連携、状態メッセージ | 正式実装 |
| `app/services/doc_modes/multi_format_bookrag_mode.py` | Multi-Format BookRAG の段階処理、作成連携、状態更新 | 正式実装 |
| `app/services/doc_modes/constants.py` | モード間で共有する入力名、上限、フォーム値収集 | 共通契約 |
| `app/services/doc_modes/ui_fields.py` | モード別設定フィールドのメタデータ、配布モデルカタログと非公開上書きのマージ | UI 契約、Unstructured モデル契約 |
| `app/services/doc_modes/messages.py` | 完了情報の文言組立て | 表示補助 |
| `app/services/doc_modes/common.py` | モード共通 UI 情報 | 共通補助 |
| `app/services/doc_modes/multi_format.py` | 旧 Multi-Format adapter | 非活動候補 |
| `app/services/doc_modes/multi_format_bookrag.py` | 旧 BookRAG adapter | 非活動候補 |

`registry.py` は `_mode.py` の二つを登録している。無接尾辞の旧 adapter は現行アプリと試験から参照されないため、新規変更を加えない。削除は import 全検索、パッケージ利用確認、関連回帰試験を経て別変更で行う。

## 11. BookRAG サービス群

| モジュール | 単一責務 | 主な試験 |
|---|---|---|
| `app/services/bookrag_schema.py` | 物理テーブル名、DDL、スキーマ契約 | `test_bookrag_schema.py` |
| `app/services/bookrag_storage.py` | BookRAG 行の挿入、更新、読出し | `test_bookrag_storage.py` |
| `app/services/bookrag_tree.py` | 文書、節、ブロック、ノード階層の構築 | `test_bookrag_tree.py` |
| `app/services/bookrag_graph.py` | entity、link、relation グラフの構築 | `test_bookrag_graph.py` |
| `app/services/bookrag_section_rules.py` | 節判定ルールの読み書きと適用 | BookRAG 管理試験 |
| `app/services/bookrag_document_metadata.py` | 文書メタデータ、公開日、統制フィールド | `test_bookrag_document_metadata.py` |
| `app/services/bookrag_document_relations.py` | 文書間関係の初期化、検証、CRUD、入出力 | `test_bookrag_document_relations.py` |
| `app/services/bookrag_integrity.py` | 表間件数、孤児、キー、検索可能性の検査 | `test_bookrag_integrity.py` |
| `app/services/bookrag_reconcile.py` | 既存表と期待状態の差分修復 | `test_bookrag_reconcile.py` |
| `app/services/bookrag_query_planner.py` | 質問から検索計画・facet・scope を生成 | `test_bookrag_query_planner.py` |
| `app/services/bookrag_retrieval_policy.py` | 検索量、範囲、閾値等の方針 | `test_bookrag_retrieval_policy.py` |
| `app/services/bookrag_retrieval.py` | 基本検索と Evidence Package 構築 | `test_bookrag_retrieval.py` |
| `app/services/bookrag_adaptive_retrieval.py` | 候補探索、再順位付け、多様化、coverage 判定 | `test_bookrag_adaptive_retrieval.py` |

- `DD-MOD-BR-001`：スキーマ名と列契約は `bookrag_schema.py` を正本とする。
- `DD-MOD-BR-002`：検索は raw SQL 結果を直接 API 応答にせず Evidence Package へ正規化する。
- `DD-MOD-BR-003`：管理用メタデータと文書関係は作成パイプラインから独立して再編集可能とする。
- `DD-MOD-BR-004`：BookRAG 判定は名前推測ではなく Description の正式マーカーと実データを使用する。

## 12. ユーティリティ

| モジュール | 責務 | 制約 | 主な試験 |
|---|---|---|---|
| `app/utils/uploads.py` | アップロード名の安全化、許可拡張子、パス境界、保存 | 保存先 root 外へ出ない | `test_upload_lifecycle.py` |
| `app/utils/table_state.py` | 表示表の選択・列・行状態に関する小さな変換 | 外部 I/O を持たない | フロントエンド関連試験 |

ユーティリティは無関係な関数の置き場にしない。業務用語を持つ処理は対応サービスへ置く。

## 13. コマンドと保守スクリプト

| 入口 | 責務 | 設計上の扱い |
|---|---|---|
| `uv run uvicorn app.main:create_app --factory` | Web アプリの唯一の通常起動 | 単一プロセスで起動する |
| `python -m app.db` | migrate、status、backup | SQLite 管理専用 |
| `app/ops/__main__.py`（`python -m app.ops`） | 保守ジョブの登録・実行 | 破壊操作は明示フラグを必要とする |
| `scripts/check_direct_dependencies.py` | import と直接依存の整合 | 依存追加時に必須 |
| `scripts/check_publication.py` | 公開禁止情報と公開物の検査 | push 前に必須 |
| `scripts/check_doc_parity.py` | 公開英日文書の構造・対応検査 | 公開文書変更時に必須 |
| `scripts/smoke_test.py` | 基本 HTTP 動作 | 配布前確認 |
| `scripts/browser_action_test.py` | 主要画面操作 | UI・ルーター変更時に必須 |
| `scripts/live_smoke_test.py` | 実 Teradata 接続を使う任意試験 | 明示設定時だけ実行 |

## 14. 変更種別から配置先を決める表

各パッケージの `__init__.py` は名前空間と限定的な再 export だけを担い、業務処理を実装しない。初期化時 I/O、暗黙の接続、ジョブ開始を追加してはならない。

| 変更内容 | 第一配置先 | 同時確認 |
|---|---|---|
| 環境変数、起動条件 | `core/settings.py` | main、運用文書、settings test |
| ログイン、ロール、セッション | AuthStore / repository | security、auth router、DB migration |
| 接続プロファイル、PEM、PAT | AuthStore / CredentialVault / repository | system_admin、runtime、秘密値試験 |
| 新しい Web 操作 | 対応 workflow/service | web router、UI_DESIGN、browser test |
| 新しい JSON API | service + `routers/api.py` | Pydantic schema、auth、API test |
| Vector Store 管理項目 | `vector_management.py` | SDK 契約、table state、UI test |
| 文書処理段階 | 専用 service / workflow job | manifest、artifact、再試行試験 |
| BookRAG テーブル列 | `bookrag_schema.py` と storage | integrity、retrieval、migration 方針 |
| BookRAG 検索規則 | planner / policy / retrieval | API contract、evidence、回帰データ |
| 長時間処理 | JobRepository + handler | heartbeat、cancel、restart、artifact |
| SQLite 列または表 | migration + repository | backup、旧 DB からの移行試験 |
| 外部 SDK 仕様差 | integration/service 境界 | 固定バージョン、契約試験 |
| CSS、HTML、メッセージ | template/static/router context | `13_UI_DESIGN.md`、browser test |

## 15. モジュール分割判断

次のいずれかに該当するとき、既存ファイルへの追記ではなく分割を検討する。

1. 同じファイルに二つ以上の外部 I/O 境界がある。
2. 変更理由が互いに独立した機能群を含む。
3. 単体試験で外部依存を切り離せない。
4. 一つの関数が入力検証、永続化、外部 API、表示生成を同時に行う。
5. 循環 import を避けるため遅延 import が増える。
6. 障害時の再試行単位と関数境界が一致しない。

分割自体を目的にしない。公開関数、ジョブ kind、payload、マニフェスト、SQLite、API の互換性を保ち、各段階でテスト可能にする。

## 16. モジュール廃止手順

1. Python import、文字列 import、テンプレート、スクリプト、文書、パッケージ成果物を全検索する。
2. 正式代替入口を確認する。
3. 非活動を示す回帰試験または参照検査を追加する。
4. 対象だけを削除し、互換性目的の再 export が必要か判断する。
5. 全テスト、wheel 検証、公開検査を実行する。
6. 本書と INDEX を更新する。

## 17. 完了条件

モジュール変更は次をすべて満たしたとき完了とする。

- 責務と依存方向が本書に一致する。
- 外部入力、秘密値、状態所有者、失敗時動作が明示されている。
- 正常系、入力不正、権限不正、外部障害、再実行の必要な試験がある。
- UI、API、DB、ジョブ、マニフェストの契約変更が関連設計書へ反映されている。
- 不要な互換層、未使用 import、重複実装を新たに増やしていない。
- 変更報告に設計 ID、対象モジュール、実行試験を記載している。
