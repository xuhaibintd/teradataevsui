# 検索・BookRAG API 詳細設計

文書 ID：`TD-EVSUI-DD-09`

## 1. 提供インターフェース

本システムは、ログイン済みブラウザー向けの Retrieval UI と、session または明示 token で利用する BookRAG JSON API を提供する。両者は同じ Teradata runtime と BookRAG service を利用し、証拠の意味を一致させる。

## 2. モジュール

| モジュール | 責務 |
|---|---|
| `app/workflows/chat_flow.py` | UI 質問、履歴、reset |
| `app/web_support.py` | 標準 VectorStore reply と BookRAG 表示変換 |
| `app/routers/api.py` | Pydantic 契約、API auth、runtime、応答 |
| `app/services/bookrag_*retrieval*.py` | scope、検索、再構築、rerank |
| `app/services/bookrag_query_planner.py` | query plan |

## 3. Retrieval UI

UI は選択済み Vector Store、検索方式、質問を受ける。許可方式は次である。

- `vectorstore.ask`
- `vectorstore.similarity_search`
- BookRAG retrieve API

送信時にログイン、接続、資源名、質問、方式を検証する。chat history は browser session scope に置き、他ユーザーまたは別 session と共有しない。`Clear` は表示履歴を消すだけで Vector Store や永続 BookRAG データを変更しない。

## 4. HTTP API 一覧

| Method | Path | 用途 |
|---|---|---|
| GET | `/api/bookrag/schema` | table mapping と join contract |
| GET | `/api/bookrag/retrieve` | query parameter による evidence 検索 |
| POST | `/api/bookrag/retrieve` | JSON request による evidence 検索 |
| GET | `/api/bookrag/answer` | evidence と既定 grounded answer |
| POST | `/api/bookrag/answer` | JSON request による answer |
| GET | `/healthz` | application health |

GET と POST の同機能は、有効な入力に対して同じ service 処理を共有する。POST body は Pydantic model で制約違反を `422` とし、GET query の `top_k` は 1～20 に丸める。

## 5. 入力契約

| field | 型・制約 | 意味 |
|---|---|---|
| `question` | 1～4000 文字 | 検索質問 |
| `vector_store_name` | 1～256 文字 | 明示対象。必須 |
| `schema_name` | 256 文字以内、任意 | Teradata schema override |
| `top_k` | 1～20、既定 5 | 最終候補数 |

空白だけの必須値を有効とせず `422` とする。schema と資源名は SQL 識別子境界で再検証する。POST の API request body は Pydantic model を正本とする。

## 6. 認証と runtime

認証順は次である。

1. 有効なブラウザー session
2. 外部 API が有効な場合の Bearer token
3. `X-API-Key`

session mode はその session が接続済みであることを要求する。external token mode は保存済み既定接続を再活性化する。認証失敗は `401` と `WWW-Authenticate: Bearer`、未接続は `409`、接続不能は `503` とする。

## 7. Retrieve 応答

`BookRAGRetrieveResponse` は次を持つ。

- `meta`：request_id、UTC generated_at、API version、auth mode、principal、top_k
- question、vector_store_name、schema_name、top_k
- `evidence`：packages、table mapping、scope、query plan、coverage、policy
- `llm_input` 相当の下流利用情報
- UI 用 assistant message と時刻

evidence package の詳細は `11_BOOKRAG.md` に従う。API version は `bookrag-v1` とし、同 version 内で既存 field の意味を変更しない。

## 8. Answer 応答

`BookRAGAnswerResponse` は retrieve 情報に加えて次を持つ。

- `llm_input`：document、task、instructions、scope、plan、evidence、output contract
- `answer`：grounded text と citations
- `evidence`：回答に使用した正本 evidence

citations は evidence の doc_id/node_id/page/source と対応する。根拠がない場合は空 evidence を成功した断定回答として返さない。

## 9. 処理フロー

```text
request validation
  → auth context
  → runtime activation
  → VectorStore open
  → adaptive BookRAG retrieval
  → governed scope 検証
  → final evidence lock
  → response model validation
  → JSON response
```

各応答に request ID を持たせる。BookRAG の正常応答は、利用者 supplied ID を trim して先頭 128 文字まで使用し、未指定時は UUID を生成する。未処理例外の request ID は `SEC-ERROR-004` に従い、安全な文字かつ 128 文字以内の場合だけ利用者指定値を使用する。

## 10. エラー契約

| Status | 条件 |
|---:|---|
| 400 | Vector Store を開けない |
| 401 | session/token なしまたは不正 |
| 409 | session 未接続、governed document scope なし |
| 422 | 必須値の空白、入力型または Pydantic 制約、長さ、POST body の top_k 範囲外 |
| 500 | 検索処理内の予期しない失敗 |
| 503 | SDK、SQL、接続 runtime が利用不能 |

外部例外の secret を redaction する。response model validation failure は利用者入力エラーとして偽装せず、request ID 付き内部エラーとして扱う。

## 11. API 互換性

- field 追加は optional または version 更新を検討する。
- field 削除、型変更、意味変更は API version 更新を必須とする。
- schema endpoint は外部利用者が table 名を推測せず join contract を得る正本である。
- UI 専用表現を API evidence の正本へ混ぜない。
- raw SDK object を response にしない。

## 12. 性能

- top_k 上限を 20 とする。
- SQL は doc_id/node_id の集合をまとめて取得し、package ごとの N+1 query を避ける。
- current/background track の候補数は policy で制限する。
- response に不必要な raw JSON 全体を含めない。
- API timeout 後も Teradata query の状態を仮定せず、次要求で runtime を正常化する。

## 13. 検証項目

- GET/POST の同じ入力が同じ evidence 意味を返す。
- session、Bearer、X-API-Key、disabled API、誤 token を試験する。
- 400/401/409/422/500/503 と request ID を試験する。
- response model と schema endpoint の契約を固定する。
- citation が evidence key と一致し、secret と内部 path がない。
- 複数 session の active connection が交差しない。
