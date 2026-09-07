# ID・認証・セキュリティ詳細設計

文書 ID：`TD-EVSUI-DD-03`

## 1. 責務と境界

本領域は、ユーザー認証、サーバーサイドセッション、ロール認可、共有認証情報の暗号化、ブラウザー要求の保護、外部 API token、監査、および安全なエラー出力を担当する。

| モジュール | 責務 |
|---|---|
| `app/auth_store.py` | ID・設定の互換ファサード、bootstrap、監査付き管理操作 |
| `app/repositories/user_repository.py` | ユーザー参照 |
| `app/repositories/session_repository.py` | セッション発行、検証、失効 |
| `app/repositories/external_service_repository.py` | 外部サービス設定 |
| `app/services/credential_vault.py` | Fernet 暗号化、PEM 実体化 |
| `app/core/security.py` | CSRF 相当の同一生成元検証、ヘッダー、秘匿化 |
| `app/core/errors.py` | 要求 ID と安全な共通エラー |
| `app/routers/auth.py` | 初回管理者設定、login、logout |
| `app/routers/system_admin.py` | 管理者操作 |

## 2. 認証フロー

### 2.1 初回管理者設定

起動時の環境変数・旧設定 bootstrap 後も `users` が 0 件の場合、`GET /login` は `GET /setup` へ誘導する。初回設定は username、password、password confirmation を受け取り、enabled `admin` を一件だけ作成して login へ戻す。

- `AUTH-INIT-001`：`/setup` はユーザー 0 件の場合だけ利用でき、作成後の GET/POST は login へ redirect する。
- `AUTH-INIT-002`：username は通常ユーザーと同じ文字規則、password は 8 文字以上、confirmation は完全一致を必須とする。
- `AUTH-INIT-003`：0 件確認と insert は `BEGIN IMMEDIATE` transaction 内で行い、競合した二件目を拒否する。
- `AUTH-INIT-004`：password と confirmation を HTML、監査、ログ、redirect URL へ残さず、再表示しない。
- `AUTH-INIT-005`：作成した password は通常ユーザーと同じ Argon2 hash だけを保存し、`user.initial_admin` を監査する。
- `AUTH-INIT-006`：`EVSUI_BOOTSTRAP_ADMIN` / `EVSUI_BOOTSTRAP_PASSWORD` は無人デプロイ用として維持し、それで管理者が作成済みなら初回設定を表示しない。

空 DB を外部公開したまま放置すると最初の訪問者が管理者を取得できるため、初回設定は管理ネットワークまたはローカル接続で完了してから公開する。

### 2.2 通常ログイン

```text
username/password
  → users を大文字小文字非区別で検索
  → enabled と locked_until を確認
  → Argon2 ハッシュ検証
  → 失敗回数更新または成功状態リセット
  → 監査記録
  → ランダム session ID 発行
  → SHA-256 hash のみ SQLite 保存
  → HttpOnly / SameSite=Lax cookie
```

- `AUTH-001`：平文パスワードを保存しない。
- `AUTH-002`：ユーザー存在、不存在、無効、誤パスワードを画面文言で区別しない。
- `AUTH-003`：5 回相当の連続失敗で 300 秒ロックし、成功時に失敗状態をリセットする。
- `AUTH-004`：セッション ID の原文を DB、ログ、監査へ保存しない。
- `AUTH-005`：HTTPS 時は cookie に `Secure` を設定する。

## 3. セッション

`sessions` は `session_id_hash`、`user_id`、作成、最終利用、有効期限、失効時刻を持つ。取得時にユーザーの `enabled` も検証する。最終利用時刻の DB 更新は 60 秒以上経過した場合に限定し、読み取り負荷を抑える。

ブラウザーの業務状態はメモリ上の `user_sessions` にあり、認証セッションとは別物である。プロセス再起動後もログイン cookie が有効なら認証情報から新しい画面状態を作るが、以前の未永続 UI 状態は復元しない。

## 4. ロールと認可

| ロール | 意味 |
|---|---|
| `admin` | システム設定、ユーザー、全業務操作 |
| `operator` | Vector Store と BookRAG の業務変更 |
| `viewer` | 接続、参照、検索 |

- 認可は Router dependency と各重要操作のサーバー処理で確認する。
- 管理 API の POST は `admin` を必須とする。
- 共有業務データを書き換える経路は `admin` または `operator` を必須とする。
- Governance の変更・export はログインと接続状態も要求する。
- 最後の有効管理者を無効化または降格してはならない。
- ジョブは所有者本人または管理者だけが参照でき、キャンセルは operator 以上とする。

`permissions` テーブルは将来のリソース単位認可用である。現在のロール認可を、未実装の細粒度認可が存在するかのように扱わない。

## 5. シークレット保存

| シークレット | 保存 | 表示 |
|---|---|---|
| DB password | Fernet 暗号文 | 設定済み状態のみ |
| PAT | Fernet 暗号文 | 設定済み状態のみ |
| PEM | Fernet 暗号文 | ファイル名と状態のみ |
| Unstructured API key | Fernet 暗号文 | 設定済み状態のみ |
| Job secret payload | Fernet 暗号文 | 表示しない |
| External API token | 環境変数 | 表示しない |

暗号鍵は DB と別に保管する。開発では key file の自動生成を許すが、本番では事前設定を必須とする。DB と暗号鍵の一方だけをバックアップしても復旧できないため、別権限で一組として管理する。

## 6. PEM 実体化

SDK がファイルパスを要求する場合に限り、`CredentialVault.materialize_pem()` が復号済み内容を `pem_runtime/<profile_id>/` へ書き出す。ファイル名は basename 化し、許可拡張子以外は安全な既定名へ置換する。可能な OS では権限 `0600` を設定する。

- PEM 内容を `uploads/pem` の永続ソースに戻さない。
- 接続プロファイル削除時に対応する実体化ファイルを削除する。
- エラーや UI へ絶対パスと内容を出さない。

## 7. ブラウザー要求保護

状態変更メソッド `POST`、`PUT`、`PATCH`、`DELETE` に対して `Sec-Fetch-Site`、`Origin`、`Referer` を検証する。外部 `/api/` は browser CSRF 対象外だが、session または API token を要求する。

本番では Origin と Referer の両方がない変更要求を拒否する。応答には次を付与する。

- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: same-origin`
- 制限付き `Permissions-Policy`
- self 基準の Content Security Policy
- HTTPS 本番時の HSTS

## 8. 外部 API 認証

外部 API は `EVSUI_EXTERNAL_API_ENABLED=true` かつ `EVSUI_API_TOKEN` 設定時だけ利用可能とする。token は `Authorization: Bearer` または `X-API-Key` で受け、`hmac.compare_digest` で比較する。

ブラウザーの有効なログインセッションも API を利用できる。session 認証はその session の接続状態を使用し、external token は保存済み既定接続を専用に再活性化する。

## 9. 秘匿化とエラー

`redact_sensitive_data()` は辞書と配列を再帰的に走査し、機密キーを `[REDACTED]` にする。`redact_sensitive_text()` は既知の秘密値、Bearer token、代入形式を除去する。

- `SEC-ERROR-001`：外部例外を HTML または JSON へそのまま返さない。
- `SEC-ERROR-002`：未処理例外は `X-Request-ID` と安全な一般文だけを返す。
- `SEC-ERROR-003`：ログの trace も秘匿化する。
- `SEC-ERROR-004`：要求 ID は英数字と `._-`、128 文字以内だけ受け入れる。

## 10. 監査

最低限、ログイン結果、ユーザー作成、状態変更、ロール変更、パスワード再設定、共有接続設定、外部サービス設定を `audit_logs` へ記録する。監査詳細へシークレットを含めない。失敗操作も結果と対象を記録する。

## 11. 検証項目

- パスワードハッシュ、セッション hash、lockout、TTL、失効を確認する。
- 3 ロールの全変更経路をサーバー側で検証する。
- cross-site POST と本番 origin 不明 POST が `403` になる。
- HTML、JSON、ログ、job payload/result、export に秘密値がない。
- DB と key の組合せで暗号文を復号でき、片方だけではできない。
- external API disabled、誤 token、正 token、browser session を区別する。
