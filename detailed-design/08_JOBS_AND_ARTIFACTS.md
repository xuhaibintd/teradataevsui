# 永続ジョブ・成果物詳細設計

文書 ID：`TD-EVSUI-DD-10`

## 1. 目的

HTTP 要求を越えて存続する文書解析、CSV 生成、Teradata load、Vector Store 作成、および保守処理を SQLite の永続 job として実行する。別 Worker サービスは置かず、FastAPI lifespan が一つの `ApplicationJobRunner` を所有する。

## 2. Job 種別

| kind | 処理 |
|---|---|
| `bookrag.documents.parse` | BookRAG 文書解析 |
| `bookrag.csv.generate` | BookRAG JSON→CSV |
| `bookrag.csv.load` | BookRAG table load |
| `multi_format.documents.parse` | Multi-Format 文書解析 |
| `multi_format.csv.generate` | Multi-Format JSON→CSV |
| `multi_format.csv.load` | Multi-Format table load |
| `vector_store.create` | Vector Store 作成と Ready 確認 |
| `artifact.cleanup` | 期限切れ制品の preview または削除 |

未知 kind を登録・実行しない。kind 追加時は handler、label、payload schema、権限、result rendering、test を追加する。

## 3. Job データ契約

| field | 意味 |
|---|---|
| `id` | UUID |
| `kind` | handler key |
| `status` | queued/running/succeeded/failed/cancelled |
| `owner_user_id` | 所有者 |
| `connection_profile_id` | Teradata 対象 |
| `payload_json` | 非秘密 command |
| `secret_payload_ciphertext` | 暗号化秘密 command |
| `result_json` | redaction 済み結果 |
| `error` | redaction 済み要約 |
| `progress` | 0～100 |
| `attempt` | claim 世代 |
| timestamps | created、started、heartbeat、finished、updated |

- `JOB-001`：通常 payload に password、PAT、PEM、API key を入れない。
- `JOB-002`：result と error は保存前に再帰的に redaction する。
- `JOB-003`：handler は payload の `_job` context から owner、profile、attempt を得る。
- `JOB-004`：result は UI 復元に必要な summary を構造化して持つ。

## 4. 登録と認可

Router はログイン、role、接続、入力、参照する run manifest を検証してから job を作る。owner_user_id と connection_profile_id をサーバー側で設定し、form の所有者値を信頼しない。

job status は所有者本人と admin だけが参照できる。cancel は queued job を対象とし、operator または admin を要求する。running の外部処理を UI cancel で強制終了したと表示しない。

## 5. Claim と実行

```text
queued
  → claim_next transaction
      status=running
      attempt += 1
  → payload + decrypted secret payload
  → handler(payload, heartbeat)
  → succeeded(result) または failed(error,result?)
```

`claim_next()` は handler が存在する kind だけを取得する。実行中は専用 thread が 30 秒ごとに heartbeat を保存する。handler の progress 更新も同じ expected attempt で fence する。

## 6. Application runner

runner は 1 秒ごとに job を確認し、Teradata runtime manager の lock 内で一件ずつ worker を thread 実行する。job が連続して存在する場合は待機せず次を処理する。

停止時は stop event を設定し、runner task の終了を待つ。強制停止では DB heartbeat が残り、次回 recovery の対象になる。

## 7. Stale recovery と fencing

起動時と定期的に、heartbeat が `job_stale_seconds` より古い running job を queued へ戻す。再 claim で attempt が増える。古い handler の heartbeat、success、fail は expected attempt が一致しないため更新できず `JobClaimLost` となる。

recovery は外部副作用を巻き戻さない。CSV load または create の再実行前に manifest と Teradata 状態を照合する。破壊的処理を自動再試行しない。

## 8. 部分結果

文書ごとの成功・失敗を利用者へ見せる必要がある場合、handler は `JobExecutionError` に安全な result を付ける。job status は failed でも result summary を保持し、画面は成功ファイル、失敗ファイル、run error を表示できる。

## 9. 制品台帳

`ArtifactLifecycle` は `uploads/` を root とし、その配下の file だけを登録・削除する。

登録属性は kind、size、任意 SHA-256、metadata、job、owner、expires_at である。workflow summary 内の `*_path`、JSON file、CSV file を解決し、実在する file を job output として登録する。

- `ART-001`：root 外 path を拒否する。
- `ART-002`：台帳にない file を期限 cleanup で削除しない。
- `ART-003`：削除後に `deleted_at` を記録する。
- `ART-004`：retention は最小 1 日とする。
- `ART-005`：hash が必要な監査制品だけ内容 SHA-256 を計算する。

## 10. Cleanup

cleanup は既定で dry-run とし `would-delete` を返す。実削除には `EVSUI_ARTIFACT_CLEANUP_ENABLED=true` と明示 `--apply` または apply job payload の両方を要求する。

path 解決が root 外の場合は `blocked` とし、削除しない。file が既にない場合は missing_ok として台帳を削除済みにできる。

## 11. Job UI polling

`GET /ui/jobs/{id}` は job status と progress を返し、terminal 状態では workflow ごとの結果 partial へ変換する。polling endpoint は Teradata runtime lock を取得しない。

同じ job を複数タブが poll しても状態を変更しない。terminal result rendering は session state へ必要な create result、upload、manifest status を一度反映しても冪等であること。

- `JOB-UI-001`：JSON→CSV job は開始時を 10、文書変換の完了数を 10～90、変換後整理を 95、terminal success を 100 として heartbeat を更新する。
- `JOB-UI-002`：成功した `GET /ui/jobs/{id}` は高頻度の定常 poll のため Uvicorn access log から除外する。非 200 応答、status 以外の path、状態変更要求は除外しない。
- `JOB-UI-003`：JSON→CSV job の登録前に、同一 target database と Vector Store 名の load 済み run が存在しないことを確認する。競合時は job を登録せず、対象名の変更を求める。

## 12. 検証項目

- 各 8 kind の登録、handler、結果を試験する。
- 二重 claim、30 秒 heartbeat、stale recovery、attempt fencing を試験する。
- owner/admin status access と cancel role を試験する。
- secret payload 暗号化と result/error redaction を確認する。
- 部分失敗 result を UI が保持する。
- root 外 cleanup、dry-run、apply gate、既消滅 file を試験する。
- 再起動後に queued/terminal job と artifact inventory が保持される。
