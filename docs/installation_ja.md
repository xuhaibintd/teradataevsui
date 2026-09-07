# 初回インストールガイド

> **言語:** [English](installation.md) | 日本語
<!-- Source-SHA256: 847dbd8afe5d3dcaa87935f1b1cc6d65195b65d472bd700f74825c5f24639d12 -->

このガイドは、空の Windows AMD64 または Linux x86-64 マシンから開始し、teradataevsui を実行できる状態まで進めます。開発環境または単一ホストへの導入にはネイティブ手順を使用します。Docker をデプロイ境界とする場合は Docker Compose を使用します。

## 1. 事前準備

次を準備してください。

- `https://github.com/xuhaibintd/teradataevsui.git` を clone する権限。
- GitHub と、`uv` が使用する Python パッケージインデックスへのネットワークアクセス。
- Teradata の host、database username と password、UES URL、PAT、および必要な PEM file。
- `Multi-Format` または `Multi-Format BookRAG` を使用する場合のみ、Unstructured API URL と key。
- 本番インスタンスをローカルマシン外へ公開する前の、信頼できる HTTPS reverse proxy。

サポートするネイティブ実行環境は Python 3.11 と `uv` 0.12.10 です。`uv` が Python 3.11 をインストールして管理できるため、system Python を別途インストールする必要はありません。Git と `uv` のインストールについては、[Git](https://git-scm.com/install/) と [uv](https://docs.astral.sh/uv/getting-started/installation/) も参照してください。

## 2. Windows PowerShell でのインストール

### 2.1 Git、uv、Python 3.11 のインストール

WinGet または Git 公式インストーラーで Git をインストールします。

```powershell
winget install --id Git.Git -e
```

リポジトリで固定された版の `uv` をインストールします。`uv` がすぐ `PATH` に見つからない場合は、新しい PowerShell を開いてください。

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/0.12.10/install.ps1 | iex"
uv --version
uv python install 3.11
uv python find 3.11
```

### 2.2 Clone と実行環境の同期

```powershell
git clone https://github.com/xuhaibintd/teradataevsui.git
Set-Location teradataevsui
git rev-parse --show-toplevel
uv sync --locked --no-dev
uv run --locked --no-sync python -c "import sys; print(sys.version)"
```

固定同期を場当たり的な `pip install` に置き換えないでください。このコマンドは `.venv` を作成し、`uv.lock` から Windows 用の正確な依存関係をインストールします。

### 2.3 ローカル開発インスタンスの起動

```powershell
uv run --locked --no-sync python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8010
```

別の PowerShell で次を実行します。

```powershell
Invoke-RestMethod http://127.0.0.1:8010/healthz
```

`http://127.0.0.1:8010` を開きます。空のインストールは `/setup` へ redirect されるので、そこで最初の administrator を作成します。初期 password や default password はありません。

### 2.4 本番相当のネイティブ起動

メモリ上に Fernet key を生成し、`--reload` を付けずに起動します。

```powershell
$env:EVSUI_CREDENTIAL_KEY = uv run --locked --no-sync python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
$env:EVSUI_ENVIRONMENT = "production"
$env:WEB_CONCURRENCY = "1"
uv run --locked --no-sync python -m uvicorn app.main:app --host 127.0.0.1 --port 8010 --workers 1
```

生成した key は、その PowerShell process 内だけに存在します。永続 service では、同じ値を OS またはデプロイ用 secret store に保存し、`data/evsui.db` と一緒にバックアップしてください。外部アクセスを許可する前に、loopback listener の前段へ信頼できる HTTPS reverse proxy を配置します。

## 3. Linux x86-64 でのインストール

### 3.1 Git、curl、uv、Python 3.11 のインストール

Debian または Ubuntu の場合：

```bash
sudo apt-get update
sudo apt-get install -y git curl ca-certificates
```

RHEL 系 distribution の場合：

```bash
sudo dnf install -y git curl ca-certificates
```

リポジトリで固定された版の `uv` をインストールし、Python 3.11 を管理させます。

```bash
curl -LsSf https://astral.sh/uv/0.12.10/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv --version
uv python install 3.11
uv python find 3.11
```

### 3.2 Clone と実行環境の同期

```bash
git clone https://github.com/xuhaibintd/teradataevsui.git
cd teradataevsui
git rev-parse --show-toplevel
uv sync --locked --no-dev
uv run --locked --no-sync python -c "import sys; print(sys.version)"
```

このコマンドは `.venv` を作成し、`uv.lock` から Linux x86-64 用の正確な依存関係をインストールします。

### 3.3 ローカル開発インスタンスの起動

```bash
uv run --locked --no-sync python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8010
```

別の shell で次を実行します。

```bash
curl http://127.0.0.1:8010/healthz
```

`http://127.0.0.1:8010` を開きます。空のインストールは `/setup` へ redirect されるので、そこで最初の administrator を作成します。初期 password や default password はありません。

### 3.4 本番相当のネイティブ起動

```bash
export EVSUI_CREDENTIAL_KEY="$(uv run --locked --no-sync python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
export EVSUI_ENVIRONMENT=production
export WEB_CONCURRENCY=1
uv run --locked --no-sync python -m uvicorn app.main:app --host 127.0.0.1 --port 8010 --workers 1
```

同じ key を host の service secret store に永続化し、`data/evsui.db` と一緒にバックアップしてください。再起動方針には service manager、外部アクセスには信頼できる HTTPS reverse proxy を使用します。

## 4. ブラウザーでの初回構成

1. アプリケーション URL を開きます。user table が空の場合、**Create administrator** が表示されます。
2. username、8 文字以上の password、および同じ password の確認入力を指定します。password は Argon2 hash としてのみ保存されます。
3. 新しい account で sign in します。最初の user が作成されると、`/setup` は直ちに無効になります。
4. **System Configuration → Database Connections** を開き、環境に必要な Teradata host、username、password、UES URL、PAT、および PEM content を保存します。
5. multi-format mode が必要な場合だけ、**System Configuration → Unstructured IO** で API URL と key を保存します。
6. **Connect & Manage** へ戻り、保存した database connection を選択して接続し、**Refresh management data** を実行します。

無人初期化の場合だけ、初回起動前に `EVSUI_BOOTSTRAP_ADMIN` と `EVSUI_BOOTSTRAP_PASSWORD` の両方を設定します。ブラウザー設定を使う場合は両方を空にします。片方だけの設定は拒否され、bootstrap 値が既存 user を上書きすることはありません。

## 5. Docker Compose でのインストール

Windows では Docker Desktop、Linux では Compose plugin 付き Docker Engine をインストールします。Compose 手順では host 側の Python は不要です。

### 5.1 Windows での `.env` 準備

```powershell
git clone https://github.com/xuhaibintd/teradataevsui.git
Set-Location teradataevsui
Copy-Item .env.example .env
$bytes = New-Object byte[] 32
$rng = [Security.Cryptography.RandomNumberGenerator]::Create()
$rng.GetBytes($bytes)
$key = [Convert]::ToBase64String($bytes).Replace('+','-').Replace('/','_')
$rng.Dispose()
$key
notepad .env
```

出力された key を `EVSUI_CREDENTIAL_KEY=` にコピーします。ブラウザー初期化では二つの bootstrap 値を空にし、無人初期化では両方を入力します。

### 5.2 Linux での `.env` 準備

```bash
git clone https://github.com/xuhaibintd/teradataevsui.git
cd teradataevsui
cp .env.example .env
head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '\n'; echo
${EDITOR:-vi} .env
```

出力された key を `EVSUI_CREDENTIAL_KEY=` にコピーします。ブラウザー初期化では二つの bootstrap 値を空にし、無人初期化では両方を入力します。

### 5.3 Build、起動、確認、停止

```bash
docker compose config
docker compose up --build -d
docker compose ps
docker compose logs --follow teradataevsui
```

`http://127.0.0.1:8010/healthz` を確認し、`http://127.0.0.1:8010` を開きます。`Ctrl+C` で log follow を停止しても container は動作を続けます。service の停止には次を使用します。

```bash
docker compose down
```

bind mount された `data/`、`uploads/`、`pem_runtime/` directory は container の置換後も残ります。upgrade 時に `data/evsui.db` や credential key を削除しないでください。

## 6. 環境設定規則

| 変数 | 開発時の既定 | 本番規則 |
|---|---|---|
| `EVSUI_ENVIRONMENT` | `development` | `production` に設定 |
| `WEB_CONCURRENCY` | `1` | `1` のままにする |
| `EVSUI_DATABASE_PATH` | `data/evsui.db` | 永続 storage に配置 |
| `EVSUI_CREDENTIAL_KEY` | local key の生成を許可 | 明示した Fernet key を指定してバックアップ |
| `EVSUI_CREDENTIAL_KEY_FILE` | DB 隣接 path | 直接 key の代替。事前作成して保護 |
| `EVSUI_BOOTSTRAP_ADMIN` | 空 | 任意。bootstrap password と同時指定 |
| `EVSUI_BOOTSTRAP_PASSWORD` | 空 | 任意。指定時は 8 文字以上 |
| `EVSUI_EXTERNAL_API_ENABLED` | `false` | token access が必要な場合だけ有効化 |
| `EVSUI_API_TOKEN` | 空 | 外部 API 有効時に必須 |

Docker Compose は `.env` を自動的に読み込みます。ネイティブの PowerShell と Linux 起動は `.env` を自動では読み込まないため、service または shell 環境で必要な変数を export してください。`.env`、credential key、password、PAT、API key、PEM file、`data/`、`uploads/` は commit してはいけません。

## 7. 既存 Git checkout の更新

runtime state をバックアップし、local source の変更が上書きされないことを確認してから更新します。

```bash
uv run --locked --no-sync python -m app.db backup
git status --short
git pull --ff-only
uv sync --locked --no-dev
uv run --locked --no-sync python -m app.db migrate
```

コマンド完了後に単一 application process を再起動します。既存 database、credential key、uploads、PEM runtime directory は保持してください。バックアップと復旧の詳細は [運用](operations_ja.md) を参照してください。

## 8. インストール確認と一般的な失敗

- `uv sync --locked --no-dev` は `uv.lock` を変更せず完了する必要があります。
- `uv run --locked --no-sync python -c "import sys; print(sys.version)"` は Python 3.11 を表示する必要があります。
- `/healthz` の `{"status":"ok"}` は Web process だけを確認し、Teradata や Unstructured への接続は確認しません。
- 本当に空のインストールで `/setup` が表示されない場合は、再起動前に旧 login 値または bootstrap 変数を削除してください。setup を強制するために稼働中の本番 database を削除してはいけません。
- 本番起動で credential key 不足が報告された場合は、`EVSUI_CREDENTIAL_KEY` または明示した `EVSUI_CREDENTIAL_KEY_FILE` を設定します。
- 複数 worker が拒否された場合は、`WEB_CONCURRENCY=1` と `--workers 1` に戻します。
- `docker compose config` が key 不足を報告した場合は、`.env` の `EVSUI_CREDENTIAL_KEY` を入力します。
- login 後に Teradata または Unstructured 接続が失敗した場合は、対応する **System Configuration** を確認してください。`/healthz` はそれらの service を検査しません。
