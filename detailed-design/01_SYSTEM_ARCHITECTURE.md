# システムアーキテクチャ詳細設計

文書 ID：`TD-EVSUI-DD-01`

## 1. システムの責務

teradataevsui は、Teradata Vector Store と BookRAG の作成、検索、統制、運用を提供する FastAPI アプリケーションである。サーバーサイド Jinja2 と HTMX を主な画面配信方式とし、SQLite をコントロールプレーン、Teradata を業務データプレーン、ローカルファイルシステムを文書および中間制品の保存先として使用する。

本システムが所有するものは、ユーザー、セッション、接続プロファイル、外部サービス設定、ジョブ、制品メタデータ、および画面用セッション状態である。Vector Store と BookRAG テーブルの実体、Unstructured のリモートジョブ、および Teradata 製品の認証基盤は所有しない。

## 2. 配置構成

```text
ブラウザー
   │ HTTPS / HTML / HTMX / JSON
   ▼
FastAPI 単一プロセス
   ├─ SecurityMiddleware
   ├─ RuntimeIsolationMiddleware
   ├─ Web / Auth / Admin / Job / API Routers
   ├─ Workflows / Services
   ├─ 単一 ApplicationJobRunner
   └─ Repository / Integration Adapters
        ├─ SQLite コントロールプレーン
        ├─ Teradata + Enterprise Vector Store
        ├─ Unstructured Platform API
        └─ uploads / pem_runtime
```

## 3. レイヤーと依存方向

| レイヤー | 主なパッケージ | 責務 | 依存してよい先 |
|---|---|---|---|
| アプリケーション構成 | `app/main.py`、`app/core/` | 設定、ミドルウェア、ライフサイクル | 下位全体の組み立て |
| 配信 | `app/routers/`、`app/templates/`、`app/static/` | HTTP、HTML、JSON、入力検証 | ワークフロー、サービス |
| ワークフロー | `app/workflows/` | ユースケースの順序、状態遷移、補償 | サービス、インテグレーション |
| ドメインサービス | `app/services/` | BookRAG、文書処理、ジョブ、制品 | リポジトリ、インテグレーション |
| インテグレーション | `app/integrations/`、`app/teradata_runtime.py` | 外部 SDK の適合、契約検証 | 外部ライブラリ |
| 永続化 | `app/repositories/`、`app/db/` | SQLite とマイグレーション | 標準 DB API、暗号化サービス |

- `ARCH-001`：依存は配信からドメイン、ドメインから永続化・インテグレーションへ向ける。
- `ARCH-002`：テンプレートと JavaScript から SQLite や Teradata SDK を直接呼ばない。
- `ARCH-003`：インテグレーション固有の例外と応答形状をルーターへ漏らし続けない。安全な業務結果へ正規化する。
- `ARCH-004`：大きなサービスを分割するときも、公開ワークフロー契約と永続マニフェストを維持する。

## 4. 状態の所有者

| 状態 | 所有者 | 保存期間 | 例 |
|---|---|---|---|
| 要求状態 | FastAPI Request | 1 要求 | form、認証コンテキスト |
| ブラウザーセッション状態 | `SessionAwareState` と `user_sessions` | プロセス存続中 | 接続済み、選択中 VS、チャット履歴 |
| 認証セッション | SQLite `sessions` | TTL または失効まで | ハッシュ化セッション ID |
| コントロールプレーン | SQLite | 永続 | users、profiles、jobs、artifacts |
| SDK コンテキスト | プロセス全体 | 1 操作または接続切替まで | teradataml context、auth token |
| 業務データ | Teradata | 外部ライフサイクル | Vector Store、BookRAG tables |
| 中間・出力ファイル | `uploads/` | 制品期限まで | 原文書、JSON、CSV、manifest |
| PEM 実体化 | `pem_runtime/` | SDK 利用に必要な期間 | 復号済み一時 PEM |

ブラウザーセッション状態は永続ジョブの正本ではない。再起動を越える処理状態は SQLite `jobs` に保存する。Teradata SDK コンテキストはユーザーごとに独立していないため、要求ごとに選択プロファイルを再活性化する。

## 5. 単一プロセス設計

最新版の Teradata SDK はプロセス全体で共有されるコンテキストを使用する。本システムは `WEB_CONCURRENCY=1` を必須とし、次の二重防御を行う。

1. `Settings.validate_runtime()` が複数 Web Worker を拒否する。
2. `SingleInstanceLock` が同じ SQLite データベースを使用する複数アプリケーションプロセスを拒否する。

HTTP の Teradata 操作とアプリ内ジョブ実行器は同じ `TeradataRuntimeManager.operation()` ロックを共有する。外部の独立 Worker プロセスは使用しない。

## 6. アプリケーションライフサイクル

起動時は次の順序を守る。

1. 環境設定の読込と検証
2. FastAPI、エラーハンドラー、ミドルウェア、静的資産の構成
3. SQLite マイグレーション
4. CredentialVault、AuthStore、各 Repository の生成
5. 環境変数または旧設定による初期管理者 bootstrap、Unstructured 設定、接続プロファイルの移行
6. セッション状態とサービスの登録
7. 単一インスタンスロック取得
8. 中断ジョブの復旧と ApplicationJobRunner 開始

bootstrap 後もユーザーが 0 件の場合は起動を継続し、最初のブラウザーアクセスを一回限りの `/setup` へ誘導する。

停止時は新しいジョブ取得を止め、現在のジョブ実行ループを終了させ、SDK とファイルハンドルを解放する。外部処理を強制終了したと仮定してはならない。

## 7. 主要ユースケース

| ユースケース | 配信入口 | オーケストレーション | 主な外部状態 |
|---|---|---|---|
| 初回管理者設定 | `routers/auth.py` | `AuthStore.create_initial_admin` | SQLite user/audit |
| ログイン | `routers/auth.py` | `AuthStore` | SQLite session |
| Teradata 接続 | `routers/web.py` | runtime helper | Teradata context |
| Vector Store 管理 | `routers/web.py` | `vector_management`、`destroy_flow` | EVS |
| Vector Store 作成 | `routers/web.py` | `create_flow`、永続 job | EVS、Teradata |
| 文書処理 | `routers/web.py` | workflow job handlers | Unstructured、files、Teradata |
| 検索 | Web または API | `chat_flow`、BookRAG services | EVS、Teradata |
| システム設定 | `routers/system_admin.py` | `AuthStore` façade | SQLite |
| 保守 | CLI または maintenance job | lifecycle services | SQLite、files |

## 8. エラー境界

- 配信層は入力不正を `4xx`、外部サービス不能を `503`、競合状態を `409` として区別する。
- 未処理例外は要求 ID を付け、安全化した診断をログへ残し、利用者へ内部情報を返さない。
- ワークフローは部分結果を保持する必要がある場合、構造化結果付きの失敗としてジョブへ保存する。
- 外部操作の後にローカル保存が失敗した場合は、再試行前に外部状態を照合する。

## 9. 拡張規則

新機能は、配信入口、サービス契約、状態所有者、権限、外部副作用、永続形式、復旧方法、テストを定義してから追加する。新しい独立プロセス、メッセージブローカー、別 DB、または新 API 世代は、現在の問題を解決する具体的根拠がある場合だけ導入する。

## 10. 検証項目

- アプリが `WEB_CONCURRENCY=1` で起動し、2 プロセス目が同じ DB ロックを取得できない。
- 同時 HTTP 操作とジョブが SDK コンテキストを交差させない。
- セッションごとの接続、フォーム、チャット状態が他ユーザーへ漏れない。
- 再起動後に SQLite のユーザー、設定、ジョブ、制品が保持される。
- 未処理例外に要求 ID があり、シークレットを含まない。
