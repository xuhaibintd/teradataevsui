# Vector Store ライフサイクル詳細設計

文書 ID：`TD-EVSUI-DD-06`

## 1. 対象

本書は、接続後のヘルス確認、一覧取得、詳細補完、作成、Ready 確認、検索利用、および削除を一つの Vector Store ライフサイクルとして定義する。UI 表示規則は `13_UI_DESIGN.md`、SDK context は `07_TERADATA_RUNTIME.md` を参照する。

## 2. モジュール責務

| モジュール | 責務 |
|---|---|
| `app/services/vector_management.py` | health/list の正規化、Description 補完、選択状態 |
| `app/workflows/create_flow.py` | 作成入力の検証、payload 構築、job 登録 |
| `app/workflows/create_status.py` | SDK status 分類、source/index 行数検証 |
| `app/services/workflow_jobs.py` | VectorStore.create と Ready 待機 |
| `app/workflows/chat_flow.py` | ask/similarity_search の対話処理 |
| `app/workflows/destroy_flow.py` | 削除と BookRAG 関連テーブル cleanup |
| `app/utils/table_state.py` | 不定形 SDK 表応答の正規化 |

## 3. 資源モデル

業務画面では一つの `Vector Store` として扱い、主要属性は次とする。

| 属性 | 意味 |
|---|---|
| `name` | 接続対象内の正確な資源名 |
| `description` | 用途と BookRAG marker |
| `type` | file-based または content-based |
| `status` | READY、CREATING、FAILED 等 |
| `database` | 関連 DB/schema |
| `owner` | SDK が提供する所有者 |
| `kind` | 内部アダプター用。通常 UI には出さない |

最新版 SDK が返す JSON、表、ネスト構造をサービスで正規化し、Template が SDK alias を探索しない。

## 4. 管理更新フロー

```text
Refresh management data
  → session runtime 再活性化
  → health
  → list
  → 接続 profile に属する行へ正規化
  → 不足 Description を get_details で補完
  → state を一括更新
  → 表示
```

- `VS-LIST-001`：更新前の選択名が新一覧に存在する場合は保持し、存在しない場合だけ解除する。
- `VS-LIST-002`：Description 補完は上限付き並列処理と request 内 cache を使う。
- `VS-LIST-003`：一部詳細の `403` や失敗で一覧全体を失敗させない。該当セルは空白のままとし、管理領域に warning を残す。正常に空値が返された場合は warning を出さない。
- `VS-LIST-004`：health と list の一方が失敗した場合、利用可能な結果と失敗状態を区別する。
- `VS-LIST-005`：単なる行選択は server、SDK、DB を呼ばない。

## 5. Description と BookRAG

Description は一覧応答の alias を優先し、不足時に正式な詳細 API から取得する。取得できた空文字と取得不能はどちらもセルを空白にするが、取得不能の場合だけ管理領域に warning を表示して区別する。

`unstructured_bookrag_flg` は BookRAG 識別 marker である。Description は marker を含めて応答内容を改変せず表示する。BookRAG Governance の対象判定は、この marker または作成マニフェストの確定情報を使い、資源名の文字列規則で推測しない。

## 6. 作成入力契約

必須入力は次である。

- `vector_store_name`
- `doc_pipeline_mode`
- `embeddings_model`
- upload、`document_files`、または検証済み loaded CSV run のいずれか一つ

作成 field は `create_config.py` の定義から型変換する。CSV 型、Boolean、整数、非負整数、float を区別する。UI に表示していない field や対象モードで無効な field を SDK payload へ混入させない。

## 7. 作成シーケンス

```text
入力検証
  → mode handler 選択
  → 既存資源の exact match 確認
  → mode preprocess
  → create payload 安全化
  → durable vector_store_create job
      → profile 再活性化
      → 必要なら文書テーブル準備
      → VectorStore(name).create(**payload)
      → status polling
      → source/index row count 検証
      → manifest status 更新
      → job result
```

- 既存資源を exact match で確認し、同名作成を事前に拒否する。
- 一覧確認が失敗しただけで「存在しない」と断定しない。
- SDK が既存エラーを返した場合、外部状態を壊す cleanup を行わない。
- create call preview と result JSON は secret redaction 後だけ保存・表示する。

## 8. Ready と整合性確認

`create_status.py` は SDK 応答を `ready`、`failed`、`pending`、`unknown` に分類する。poll interval と timeout は Settings から得る。

Ready になった後、可能な場合は次を検証する。

- source table の非空 embedding 対象行数
- `vectorstore_<name>_index` の行数
- source と index の期待件数一致
- BookRAG の場合は `bnode.content` の対象数と index 数

status timeout は remote create の取消を意味しない。結果は pending として返し、一覧または status で後から確認する。

## 9. 検索利用

標準検索は選択した資源を `VectorStore(name)` として開き、許可された `ask` または `similarity_search` だけを実行する。BookRAG は `11_BOOKRAG.md` の証拠再構築を通る。

質問長、資源名、top_k を検証する。選択時に VectorStore を初期化せず、送信時にのみ runtime を使用する。

## 10. 削除

削除は対象名、内部 kind、role を再検証してから実行する。成功後に一覧と選択を更新する。

BookRAG 資源では、Vector Store 削除に関連する BookRAG table/view の cleanup が必要になる。cleanup 対象は schema contract から導出し、名前の手書きリストを複数箇所へ持たない。外部削除と table cleanup のどちらかが失敗した場合は、実際に完了した操作を記録し、再試行時に存在確認する。

`409` の作成・更新・削除中、`403` の権限不足、`404` 相当の既消滅を区別する。既消滅を成功扱いにする場合は、関連テーブルと一覧状態も照合する。

## 11. 冪等性と再試行

| 操作 | 自動再試行 | 理由 |
|---|---|---|
| health/list/details | 制限付き可 | 読み取り |
| status polling | 可 | 読み取り |
| create | 不可 | 重複・部分作成の可能性 |
| destroy | 不可 | 競合・部分 cleanup の可能性 |
| ask/search | 利用者再送 | 計算負荷と結果差異 |

変更操作の再実行は、同名資源、status、対象 table、manifest を確認してから新しい job として行う。

## 12. 検証項目

- SDK 応答形状の alias と nested JSON を正規化できる。
- Description の正常な空値と取得不能はいずれも空白セルとし、取得不能の場合だけ warning が表示される。
- 同名作成、Ready、Failed、Pending、index 空、不一致を試験する。
- create payload に非対象 mode field と secret がない。
- 削除成功、409、403、既消滅、BookRAG cleanup 部分失敗を試験する。
- session/profile を跨いで別接続の一覧や資源を操作しない。
