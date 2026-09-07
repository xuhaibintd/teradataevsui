from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.fernet import InvalidToken

from app.db.migrations import run_migrations
from app.db.sqlite import SQLiteDatabase
from app.repositories import (
    ArtifactRepository,
    ExternalServiceRepository,
    JobRepository,
    SessionRepository,
    UserRepository,
)
from app.services.credential_vault import CredentialVault


AUTH_ROLES = ("admin", "operator", "viewer")
PASSWORD_HASHER = PasswordHasher()
SESSION_TTL_SECONDS_DEFAULT = 8 * 60 * 60
CONNECTION_CONFIG_TEXT_LIMITS = {
    "host": 512,
    "username": 128,
    "password": 8192,
    "ues_url": 2048,
    "pat_token": 8192,
    "pem_file": 2048,
}
PEM_CONTENT_MAX_BYTES = 1024 * 1024


@dataclass(frozen=True)
class AuthPrincipal:
    user_id: int
    username: str
    display_name: str
    role: str
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def hash_password(password: str) -> str:
    value = str(password or "")
    if len(value) < 8:
        raise ValueError("Password must contain at least 8 characters.")
    return PASSWORD_HASHER.hash(value)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return bool(PASSWORD_HASHER.verify(str(password_hash or ""), str(password or "")))
    except (InvalidHashError, VerifyMismatchError, TypeError, ValueError):
        return False


def _clean_username(raw: str) -> str:
    username = str(raw or "").strip()
    if not username or len(username) > 128:
        raise ValueError("Username must contain 1 to 128 characters.")
    if any(char.isspace() for char in username):
        raise ValueError("Username cannot contain whitespace.")
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+", username):
        raise ValueError("Username may contain only letters, numbers, '.', '_', '@', and '-'.")
    return username


def _clean_role(raw: str) -> str:
    role = str(raw or "viewer").strip().lower()
    if role not in AUTH_ROLES:
        raise ValueError(f"Role must be one of: {', '.join(AUTH_ROLES)}.")
    return role


class AuthStore:
    """SQLite-backed users and server-side sessions.

    Connections are deliberately short lived, so the store is safe to use from
    FastAPI worker threads. WAL and busy_timeout keep small concurrent auth and
    audit writes predictable.
    """

    def __init__(
        self,
        database_path: Path,
        *,
        session_ttl_seconds: int = SESSION_TTL_SECONDS_DEFAULT,
        pem_runtime_dir: Path | None = None,
        legacy_file_root: Path | None = None,
        credential_key: str = "",
        credential_key_file: Path | None = None,
        allow_generated_credential_key: bool = True,
    ) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        self.session_ttl_seconds = max(300, int(session_ttl_seconds))
        self.pem_runtime_dir = Path(pem_runtime_dir or (self.database_path.parent / "pem_runtime")).expanduser().resolve()
        self.legacy_file_root = Path(legacy_file_root or self.database_path.parent).expanduser().resolve()
        self.credential_key = str(credential_key or "").strip()
        self.credential_key_file = (
            Path(credential_key_file).expanduser().resolve()
            if credential_key_file is not None
            else None
        )
        self.allow_generated_credential_key = bool(allow_generated_credential_key)
        self.database = SQLiteDatabase(self.database_path)
        self.credential_vault = CredentialVault(
            database_path=self.database_path,
            runtime_dir=self.pem_runtime_dir,
            credential_key=self.credential_key,
            credential_key_file=self.credential_key_file,
            allow_generated_key=self.allow_generated_credential_key,
        )
        self.users = UserRepository(self.database)
        self.sessions = SessionRepository(
            self.database,
            ttl_seconds=self.session_ttl_seconds,
        )
        self.jobs = JobRepository(self.database, credential_vault=self.credential_vault)
        self.artifacts = ArtifactRepository(self.database)
        self.external_services = ExternalServiceRepository(self.database, self.credential_vault)

    def _connect(self) -> sqlite3.Connection:
        return self.database.connect()

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            run_migrations(connection)

    def count_users(self) -> int:
        return self.users.count()

    def count_enabled_admins(self) -> int:
        return self.users.count_enabled_admins()

    def create_user(
        self,
        *,
        username: str,
        password: str | None = None,
        password_hash: str | None = None,
        display_name: str = "",
        role: str = "viewer",
        enabled: bool = True,
        replace: bool = False,
    ) -> AuthPrincipal:
        clean_username = _clean_username(username)
        clean_role = _clean_role(role)
        encoded_password = str(password_hash or "").strip()
        if not encoded_password:
            encoded_password = hash_password(str(password or ""))
        elif not encoded_password.startswith("$argon2"):
            raise ValueError("Imported password_hash must use Argon2.")
        now = int(time.time())
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT id FROM users WHERE username=? COLLATE NOCASE", (clean_username,)
            ).fetchone()
            if existing and not replace:
                raise ValueError(f"User '{clean_username}' already exists.")
            if existing:
                connection.execute(
                    """UPDATE users SET display_name=?, password_hash=?, role=?, enabled=?, updated_at=?
                       WHERE id=?""",
                    (str(display_name).strip(), encoded_password, clean_role, int(enabled), now, existing["id"]),
                )
                user_id = int(existing["id"])
                connection.execute(
                    "UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                    (now, user_id),
                )
            else:
                cursor = connection.execute(
                    """INSERT INTO users(username, display_name, password_hash, role, enabled, created_at, updated_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?)""",
                    (clean_username, str(display_name).strip(), encoded_password, clean_role, int(enabled), now, now),
                )
                user_id = int(cursor.lastrowid)
            self._audit_with_connection(
                connection,
                user_id=user_id,
                username=clean_username,
                action="user.upsert",
                resource=clean_username,
                result="ok",
            )
        return AuthPrincipal(user_id, clean_username, str(display_name).strip(), clean_role, bool(enabled))

    def create_initial_admin(self, *, username: str, password: str) -> AuthPrincipal:
        """Create the one-time initial administrator while the user table is empty."""
        clean_username = _clean_username(username)
        encoded_password = hash_password(password)
        now = int(time.time())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT COUNT(*) AS value FROM users").fetchone()
            if int(row["value"] if row else 0) != 0:
                raise RuntimeError("Initial administrator is already configured.")
            cursor = connection.execute(
                """INSERT INTO users(username, display_name, password_hash, role, enabled, created_at, updated_at)
                   VALUES(?, '', ?, 'admin', 1, ?, ?)""",
                (clean_username, encoded_password, now, now),
            )
            user_id = int(cursor.lastrowid)
            self._audit_with_connection(
                connection,
                user_id=user_id,
                username=clean_username,
                action="user.initial_admin",
                resource=clean_username,
                result="ok",
            )
        return AuthPrincipal(user_id, clean_username, "", "admin", True)

    def bootstrap(self, legacy_users: dict[str, str] | None = None) -> int:
        if self.count_users():
            return 0
        candidates: list[tuple[str, str, bool]] = []
        bootstrap_username = str(os.getenv("EVSUI_BOOTSTRAP_ADMIN", "")).strip()
        bootstrap_password = str(os.getenv("EVSUI_BOOTSTRAP_PASSWORD", ""))
        if bool(bootstrap_username) != bool(bootstrap_password):
            raise RuntimeError("EVSUI_BOOTSTRAP_ADMIN and EVSUI_BOOTSTRAP_PASSWORD must be set together.")
        if bootstrap_username:
            if len(bootstrap_password) < 8:
                raise RuntimeError("EVSUI_BOOTSTRAP_PASSWORD must contain at least 8 characters.")
            candidates.append((bootstrap_username, bootstrap_password, True))
        candidates.extend(
            (str(username), str(password), False)
            for username, password in dict(legacy_users or {}).items()
            if str(username).strip() and str(password)
            and str(username).strip().lower() != bootstrap_username.lower()
        )
        created = 0
        for username, password, enforce_length in candidates:
            self.create_user(
                username=username,
                password=password if enforce_length else None,
                password_hash=None if enforce_length else PASSWORD_HASHER.hash(password),
                role="admin" if created == 0 else "operator",
            )
            created += 1
        return created

    def authenticate(self, username: str, password: str) -> AuthPrincipal | None:
        clean_username = str(username or "").strip()
        now = int(time.time())
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username=? COLLATE NOCASE", (clean_username,)
            ).fetchone()
            if row is None or not bool(row["enabled"]):
                self._audit_with_connection(
                    connection,
                    username=clean_username,
                    action="login",
                    result="denied",
                    detail="unknown or disabled user",
                )
                return None
            locked_until = int(row["locked_until"] or 0)
            if locked_until > now:
                self._audit_with_connection(
                    connection,
                    user_id=int(row["id"]),
                    username=str(row["username"]),
                    action="login",
                    result="locked",
                )
                return None
            if not verify_password(str(row["password_hash"]), password):
                # Increment and lock in one statement.  Computing the next value
                # from the previously selected row lets concurrent failures
                # overwrite each other and can even clear a lock written by a
                # different request.
                connection.execute(
                    """UPDATE users
                       SET failed_login_count=CASE
                               WHEN failed_login_count >= 4 THEN 0
                               ELSE failed_login_count + 1
                           END,
                           locked_until=CASE
                               WHEN failed_login_count >= 4 THEN ?
                               ELSE locked_until
                           END,
                           updated_at=?
                       WHERE id=? AND (locked_until IS NULL OR locked_until<=?)""",
                    (now + 300, now, row["id"], now),
                )
                self._audit_with_connection(
                    connection,
                    user_id=int(row["id"]),
                    username=str(row["username"]),
                    action="login",
                    result="denied",
                    detail="invalid password",
                )
                return None
            connection.execute(
                """UPDATE users SET failed_login_count=0, locked_until=NULL,
                   last_login_at=?, updated_at=? WHERE id=?""",
                (now, now, row["id"]),
            )
            self._audit_with_connection(
                connection,
                user_id=int(row["id"]),
                username=str(row["username"]),
                action="login",
                result="ok",
            )
            return self._principal_from_row(row)

    def create_session(self, principal: AuthPrincipal) -> str:
        return self.sessions.create(principal.user_id)

    def get_session(self, session_id: str, *, touch: bool = True) -> AuthPrincipal | None:
        row = self.sessions.get(session_id, touch=touch)
        return self._principal_from_row(row) if row is not None else None

    def revoke_session(self, session_id: str) -> None:
        self.sessions.revoke(session_id)

    def get_unstructured_config(self) -> dict[str, Any]:
        return self.external_services.get("unstructured") or {
            "service_name": "unstructured",
            "api_url": "",
            "api_key": "",
            "api_key_configured": False,
        }

    def save_unstructured_config(
        self,
        actor_user_id: int,
        *,
        api_url: str,
        api_key: str | None = None,
        clear_api_key: bool = False,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            user = connection.execute(
                "SELECT username FROM users WHERE id=? AND enabled=1 AND role='admin'",
                (int(actor_user_id),),
            ).fetchone()
        if user is None:
            raise PermissionError("Unstructured IO configuration requires an enabled administrator.")
        existing = self.get_unstructured_config()
        stored_key = "" if clear_api_key else (
            str(api_key or "").strip() or str(existing.get("api_key") or "")
        )
        saved = self.external_services.save(
            "unstructured",
            api_url=str(api_url or "").strip(),
            api_key=stored_key,
            updated_by=int(actor_user_id),
        )
        with self._connect() as connection:
            self._audit_with_connection(
                connection,
                user_id=int(actor_user_id),
                username=str(user["username"]),
                action="external_service.save",
                resource="unstructured",
                result="ok",
                detail="api_key_configured=" + str(bool(saved.get("api_key"))).lower(),
            )
        return saved

    def bootstrap_unstructured_config(self, values: dict[str, Any] | None) -> bool:
        config = values or {}
        return self.external_services.bootstrap(
            "unstructured",
            api_url=str(config.get("unstructured_api_url") or config.get("api_url") or "").strip(),
            api_key=str(config.get("unstructured_api_key") or config.get("api_key") or "").strip(),
        )

    def get_system_connection_config(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT host, username, password_ciphertext, ues_url,
                          pat_token_ciphertext, pem_file, pem_filename, pem_ciphertext
                   FROM system_connection_config WHERE config_id=1""",
            ).fetchone()
        if row is None:
            return None
        try:
            password = self._decrypt_credential(str(row["password_ciphertext"] or ""))
            pat_token = self._decrypt_credential(str(row["pat_token_ciphertext"] or ""))
            pem_payload = self._decrypt_bytes(str(row["pem_ciphertext"] or ""))
        except InvalidToken as ex:
            raise RuntimeError("Stored connection credentials cannot be decrypted with the configured key.") from ex
        legacy_pem_file = str(row["pem_file"] or "")
        pem_filename = str(row["pem_filename"] or "").strip()
        runtime_pem_file = (
            self._materialize_system_pem(pem_payload, pem_filename)
            if pem_payload
            else legacy_pem_file
        )
        return {
            "host": str(row["host"] or ""),
            "username": str(row["username"] or ""),
            "password": password,
            "ues_url": str(row["ues_url"] or ""),
            "pat_token": pat_token,
            "pem_file": runtime_pem_file,
            "pem_filename": pem_filename or (Path(legacy_pem_file).name if legacy_pem_file else ""),
            "pem_in_database": bool(pem_payload),
        }

    def save_system_connection_config(self, actor_user_id: int, values: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize_connection_config(values)
        now = int(time.time())
        with self._connect() as connection:
            user = connection.execute(
                "SELECT username FROM users WHERE id=? AND enabled=1 AND role='admin'",
                (int(actor_user_id),),
            ).fetchone()
            if user is None:
                raise PermissionError("System connection configuration requires an enabled administrator.")
            existing = connection.execute(
                "SELECT pem_filename, pem_ciphertext FROM system_connection_config WHERE config_id=1"
            ).fetchone()
            pem_payload = values.get("pem_content")
            if pem_payload is None:
                pem_filename = str(existing["pem_filename"] or "") if existing is not None else ""
                pem_ciphertext = str(existing["pem_ciphertext"] or "") if existing is not None else ""
            else:
                if not isinstance(pem_payload, bytes):
                    raise ValueError("PEM content must be uploaded as bytes.")
                if not pem_payload:
                    raise ValueError("PEM file is empty.")
                if len(pem_payload) > PEM_CONTENT_MAX_BYTES:
                    raise ValueError("PEM file exceeds 1 MiB.")
                pem_filename = Path(str(values.get("pem_filename") or "uploaded.pem")).name
                if Path(pem_filename).suffix.lower() not in {".pem", ".key", ".crt"}:
                    raise ValueError("Only .pem, .key, and .crt files are allowed.")
                pem_ciphertext = self._encrypt_bytes(pem_payload)
                normalized["pem_file"] = ""
            password_ciphertext = self._encrypt_credential(normalized["password"])
            pat_token_ciphertext = self._encrypt_credential(normalized["pat_token"])
            connection.execute(
                """INSERT INTO system_connection_config(
                       config_id, host, username, password_ciphertext, ues_url,
                       pat_token_ciphertext, pem_file, pem_filename, pem_ciphertext,
                       updated_by, created_at, updated_at
                   ) VALUES(1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(config_id) DO UPDATE SET
                       host=excluded.host,
                       username=excluded.username,
                       password_ciphertext=excluded.password_ciphertext,
                       ues_url=excluded.ues_url,
                       pat_token_ciphertext=excluded.pat_token_ciphertext,
                       pem_file=excluded.pem_file,
                       pem_filename=excluded.pem_filename,
                       pem_ciphertext=excluded.pem_ciphertext,
                       updated_by=excluded.updated_by,
                       updated_at=excluded.updated_at""",
                (
                    normalized["host"],
                    normalized["username"],
                    password_ciphertext,
                    normalized["ues_url"],
                    pat_token_ciphertext,
                    normalized["pem_file"],
                    pem_filename,
                    pem_ciphertext,
                    int(actor_user_id),
                    now,
                    now,
                ),
            )
            self._audit_with_connection(
                connection,
                user_id=int(actor_user_id),
                username=str(user["username"]),
                action="system_connection_config.save",
                resource="default",
                result="ok",
            )
        return self.get_system_connection_config() or normalized

    def migrate_legacy_system_pem(self) -> bool:
        """Move a path-backed PEM into encrypted database storage once."""
        with self._connect() as connection:
            row = connection.execute(
                """SELECT pem_file, pem_ciphertext
                   FROM system_connection_config WHERE config_id=1"""
            ).fetchone()
            if row is None or str(row["pem_ciphertext"] or "").strip():
                return False
            legacy_hint = str(row["pem_file"] or "").strip()
            if not legacy_hint:
                return False
            legacy_path = Path(legacy_hint)
            candidates = [legacy_path] if legacy_path.is_absolute() else [
                self.legacy_file_root / legacy_path,
                self.database_path.parent / legacy_path,
            ]
            source = next((candidate for candidate in candidates if candidate.is_file()), None)
            if source is None:
                return False
            payload = source.read_bytes()
            if not payload or len(payload) > PEM_CONTENT_MAX_BYTES:
                return False
            connection.execute(
                """UPDATE system_connection_config
                   SET pem_file='', pem_filename=?, pem_ciphertext=?, updated_at=?
                   WHERE config_id=1""",
                (source.name, self._encrypt_bytes(payload), int(time.time())),
            )
        self._materialize_system_pem(payload, source.name)
        return True

    def migrate_singleton_connection_profile(self) -> bool:
        """Copy the former singleton connection into the profile table once."""
        with self._connect() as connection:
            if connection.execute("SELECT 1 FROM system_connection_profiles LIMIT 1").fetchone():
                return False
            row = connection.execute(
                """SELECT host, username, password_ciphertext, ues_url, pat_token_ciphertext,
                          pem_filename, pem_ciphertext, updated_by, created_at, updated_at
                   FROM system_connection_config WHERE config_id=1"""
            ).fetchone()
            if row is None:
                return False
            base_name = str(row["username"] or "Default Connection").strip() or "Default Connection"
            connection.execute(
                """INSERT INTO system_connection_profiles(
                       name, host, username, password_ciphertext, ues_url, pat_token_ciphertext,
                       pem_filename, pem_ciphertext, is_default, updated_by, created_at, updated_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
                (
                    base_name,
                    row["host"],
                    row["username"],
                    row["password_ciphertext"],
                    row["ues_url"],
                    row["pat_token_ciphertext"],
                    row["pem_filename"],
                    row["pem_ciphertext"],
                    row["updated_by"],
                    row["created_at"],
                    row["updated_at"],
                ),
            )
        return True

    def list_connection_profiles(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id, name, host, username, ues_url, pem_filename, is_default,
                          password_ciphertext, pat_token_ciphertext
                   FROM system_connection_profiles
                   ORDER BY is_default DESC, name COLLATE NOCASE"""
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "name": str(row["name"]),
                "host": str(row["host"]),
                "username": str(row["username"]),
                "ues_url": str(row["ues_url"]),
                "pem_filename": str(row["pem_filename"] or ""),
                "is_default": bool(row["is_default"]),
                "password_configured": bool(row["password_ciphertext"]),
                "pat_token_configured": bool(row["pat_token_ciphertext"]),
            }
            for row in rows
        ]

    def get_connection_profile(self, profile_id: int | None = None) -> dict[str, Any] | None:
        with self._connect() as connection:
            if profile_id is None:
                row = connection.execute(
                    """SELECT * FROM system_connection_profiles
                       ORDER BY is_default DESC, id LIMIT 1"""
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM system_connection_profiles WHERE id=?",
                    (int(profile_id),),
                ).fetchone()
        if row is None:
            return None
        try:
            password = self._decrypt_credential(str(row["password_ciphertext"] or ""))
            pat_token = self._decrypt_credential(str(row["pat_token_ciphertext"] or ""))
            pem_payload = self._decrypt_bytes(str(row["pem_ciphertext"] or ""))
        except InvalidToken as ex:
            raise RuntimeError("Stored connection credentials cannot be decrypted with the configured key.") from ex
        filename = str(row["pem_filename"] or "")
        pem_file = self._materialize_system_pem(pem_payload, filename, int(row["id"])) if pem_payload else ""
        return {
            "id": int(row["id"]),
            "name": str(row["name"]),
            "host": str(row["host"]),
            "username": str(row["username"]),
            "password": password,
            "ues_url": str(row["ues_url"]),
            "pat_token": pat_token,
            "pem_file": pem_file,
            "pem_filename": filename,
            "pem_in_database": bool(pem_payload),
            "is_default": bool(row["is_default"]),
        }

    def save_connection_profile(
        self,
        actor_user_id: int,
        values: dict[str, Any],
        profile_id: int | None = None,
    ) -> dict[str, Any]:
        normalized = self._normalize_connection_config(values)
        name = str(values.get("name") or "").strip()
        if not name or len(name) > 128:
            raise ValueError("Connection name must contain 1 to 128 characters.")
        now = int(time.time())
        previous_pem_filename = ""
        with self._connect() as connection:
            user = connection.execute(
                "SELECT username FROM users WHERE id=? AND enabled=1 AND role='admin'",
                (int(actor_user_id),),
            ).fetchone()
            if user is None:
                raise PermissionError("Connection profiles require an enabled administrator.")
            existing = None
            if profile_id is not None:
                existing = connection.execute(
                    "SELECT * FROM system_connection_profiles WHERE id=?", (int(profile_id),)
                ).fetchone()
                if existing is None:
                    raise ValueError("Connection profile not found.")
                previous_pem_filename = str(existing["pem_filename"] or "")
            password = normalized["password"] or (self._decrypt_credential(existing["password_ciphertext"]) if existing else "")
            pat_token = normalized["pat_token"] or (self._decrypt_credential(existing["pat_token_ciphertext"]) if existing else "")
            pem_payload = values.get("pem_content")
            if pem_payload is None:
                pem_filename = str(existing["pem_filename"] or "") if existing else ""
                pem_ciphertext = str(existing["pem_ciphertext"] or "") if existing else ""
            else:
                if not isinstance(pem_payload, bytes) or not pem_payload:
                    raise ValueError("PEM file is empty.")
                if len(pem_payload) > PEM_CONTENT_MAX_BYTES:
                    raise ValueError("PEM file exceeds 1 MiB.")
                pem_filename = Path(str(values.get("pem_filename") or "uploaded.pem")).name
                if Path(pem_filename).suffix.lower() not in {".pem", ".key", ".crt"}:
                    raise ValueError("Only .pem, .key, and .crt files are allowed.")
                pem_ciphertext = self._encrypt_bytes(pem_payload)
            make_default = bool(values.get("is_default")) or not connection.execute(
                "SELECT 1 FROM system_connection_profiles LIMIT 1"
            ).fetchone()
            if make_default:
                connection.execute("UPDATE system_connection_profiles SET is_default=0")
            encrypted_password = self._encrypt_credential(password)
            encrypted_pat = self._encrypt_credential(pat_token)
            try:
                if existing is None:
                    cursor = connection.execute(
                        """INSERT INTO system_connection_profiles(
                               name, host, username, password_ciphertext, ues_url, pat_token_ciphertext,
                               pem_filename, pem_ciphertext, is_default, updated_by, created_at, updated_at
                           ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (name, normalized["host"], normalized["username"], encrypted_password,
                         normalized["ues_url"], encrypted_pat, pem_filename, pem_ciphertext,
                         int(make_default), int(actor_user_id), now, now),
                    )
                    saved_id = int(cursor.lastrowid)
                else:
                    saved_id = int(existing["id"])
                    connection.execute(
                        """UPDATE system_connection_profiles SET
                               name=?, host=?, username=?, password_ciphertext=?, ues_url=?,
                               pat_token_ciphertext=?, pem_filename=?, pem_ciphertext=?, is_default=?,
                               updated_by=?, updated_at=? WHERE id=?""",
                        (name, normalized["host"], normalized["username"], encrypted_password,
                         normalized["ues_url"], encrypted_pat, pem_filename, pem_ciphertext,
                         int(make_default or bool(existing["is_default"])), int(actor_user_id), now, saved_id),
                    )
            except sqlite3.IntegrityError as ex:
                raise ValueError(f"Connection name '{name}' already exists.") from ex
            self._audit_with_connection(
                connection, user_id=int(actor_user_id), username=str(user["username"]),
                action="connection_profile.save", resource=name, result="ok",
            )
        if previous_pem_filename and previous_pem_filename != pem_filename:
            self._remove_materialized_profile_pem(saved_id, previous_pem_filename)
        return self.get_connection_profile(saved_id) or {}

    def delete_connection_profile(self, actor_user_id: int, profile_id: int) -> None:
        with self._connect() as connection:
            user = connection.execute(
                "SELECT username FROM users WHERE id=? AND enabled=1 AND role='admin'",
                (int(actor_user_id),),
            ).fetchone()
            if user is None:
                raise PermissionError("Connection profiles require an enabled administrator.")
            row = connection.execute(
                "SELECT name, is_default, pem_filename FROM system_connection_profiles WHERE id=?", (int(profile_id),)
            ).fetchone()
            if row is None:
                raise ValueError("Connection profile not found.")
            connection.execute("DELETE FROM system_connection_profiles WHERE id=?", (int(profile_id),))
            if bool(row["is_default"]):
                replacement = connection.execute(
                    "SELECT id FROM system_connection_profiles ORDER BY id LIMIT 1"
                ).fetchone()
                if replacement:
                    connection.execute(
                        "UPDATE system_connection_profiles SET is_default=1 WHERE id=?",
                        (int(replacement["id"]),),
                    )
            self._audit_with_connection(
                connection, user_id=int(actor_user_id), username=str(user["username"]),
                action="connection_profile.delete", resource=str(row["name"]), result="ok",
            )
        self._remove_materialized_profile_pem(int(profile_id), str(row["pem_filename"] or ""))

    def bootstrap_connection_config(self, values: dict[str, Any] | None) -> int:
        if self.get_system_connection_config() is not None:
            with self._connect() as connection:
                connection.execute("DELETE FROM connection_configs")
            return 0
        with self._connect() as connection:
            admin = connection.execute(
                "SELECT id FROM users WHERE role='admin' AND enabled=1 ORDER BY id LIMIT 1"
            ).fetchone()
            legacy = connection.execute(
                """SELECT c.host, c.username, c.password_ciphertext, c.ues_url,
                          c.pat_token_ciphertext, c.pem_file
                   FROM connection_configs c
                   JOIN users u ON u.id=c.user_id
                   ORDER BY CASE WHEN u.role='admin' AND u.enabled=1 THEN 0 ELSE 1 END,
                            c.user_id
                   LIMIT 1"""
            ).fetchone()
        if admin is None:
            return 0
        if legacy is not None:
            try:
                normalized = self._normalize_connection_config(
                    {
                        "host": legacy["host"],
                        "username": legacy["username"],
                        "password": self._decrypt_credential(str(legacy["password_ciphertext"] or "")),
                        "ues_url": legacy["ues_url"],
                        "pat_token": self._decrypt_credential(str(legacy["pat_token_ciphertext"] or "")),
                        "pem_file": legacy["pem_file"],
                    }
                )
            except InvalidToken as ex:
                raise RuntimeError("Legacy connection credentials cannot be migrated with the configured key.") from ex
        else:
            normalized = self._normalize_connection_config(values or {})
        if not any(normalized.values()):
            return 0
        self.save_system_connection_config(int(admin["id"]), normalized)
        with self._connect() as connection:
            connection.execute("DELETE FROM connection_configs")
        return 1

    def _normalize_connection_config(self, values: dict[str, Any]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for key, limit in CONNECTION_CONFIG_TEXT_LIMITS.items():
            raw = str((values or {}).get(key) or "")
            value = raw if key == "password" else raw.strip()
            if len(value) > limit:
                raise ValueError(f"Connection configuration field '{key}' exceeds {limit} characters.")
            normalized[key] = value
        return normalized

    def _credential_key_path(self) -> Path:
        return self.credential_vault.key_path()

    def _credential_cipher(self):
        return self.credential_vault.cipher()

    def _encrypt_credential(self, value: str) -> str:
        return self.credential_vault.encrypt_text(value)

    def _decrypt_credential(self, value: str) -> str:
        return self.credential_vault.decrypt_text(value)

    def _encrypt_bytes(self, value: bytes) -> str:
        return self.credential_vault.encrypt_bytes(value)

    def _decrypt_bytes(self, value: str) -> bytes:
        return self.credential_vault.decrypt_bytes(value)

    def _materialize_system_pem(self, payload: bytes, filename: str, profile_id: int | None = None) -> str:
        return self.credential_vault.materialize_pem(payload, filename, profile_id)

    def _remove_materialized_profile_pem(self, profile_id: int, filename: str) -> None:
        self.credential_vault.remove_materialized_pem(profile_id, filename)

    def list_users(self) -> list[dict[str, Any]]:
        rows = self.users.list_admin_rows()
        users = []
        for row in rows:
            item = dict(row)
            item["enabled"] = bool(item["enabled"])
            item["last_login_display"] = (
                time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(int(item["last_login_at"])))
                if item.get("last_login_at")
                else ""
            )
            users.append(item)
        return users

    def set_enabled(self, username: str, enabled: bool) -> None:
        clean_username = _clean_username(username)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, role, enabled FROM users WHERE username=? COLLATE NOCASE", (clean_username,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown user '{clean_username}'.")
            if row["role"] == "admin" and bool(row["enabled"]) and not enabled and self.count_enabled_admins() <= 1:
                raise ValueError("The last enabled administrator cannot be disabled.")
            connection.execute(
                "UPDATE users SET enabled=?, updated_at=? WHERE id=?",
                (int(enabled), int(time.time()), row["id"]),
            )
            if not enabled:
                connection.execute(
                    "UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                    (int(time.time()), row["id"]),
                )
            self._audit_with_connection(
                connection,
                user_id=int(row["id"]),
                username=clean_username,
                action="user.enabled",
                resource=clean_username,
                result="ok",
                detail=str(bool(enabled)).lower(),
            )

    def set_role(self, username: str, role: str) -> None:
        clean_username = _clean_username(username)
        clean_role = _clean_role(role)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, role, enabled FROM users WHERE username=? COLLATE NOCASE", (clean_username,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown user '{clean_username}'.")
            if (
                row["role"] == "admin"
                and bool(row["enabled"])
                and clean_role != "admin"
                and self.count_enabled_admins() <= 1
            ):
                raise ValueError("The last enabled administrator cannot be demoted.")
            connection.execute(
                "UPDATE users SET role=?, updated_at=? WHERE id=?",
                (clean_role, int(time.time()), row["id"]),
            )
            self._audit_with_connection(
                connection,
                user_id=int(row["id"]),
                username=clean_username,
                action="user.role",
                resource=clean_username,
                result="ok",
                detail=clean_role,
            )

    def reset_password(self, username: str, password: str) -> None:
        clean_username = _clean_username(username)
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE users SET password_hash=?, failed_login_count=0, locked_until=NULL, updated_at=?
                   WHERE username=? COLLATE NOCASE""",
                (hash_password(password), int(time.time()), clean_username),
            )
            if not cursor.rowcount:
                raise ValueError(f"Unknown user '{clean_username}'.")
            connection.execute(
                """UPDATE sessions SET revoked_at=? WHERE user_id=(SELECT id FROM users WHERE username=? COLLATE NOCASE)
                   AND revoked_at IS NULL""",
                (int(time.time()), clean_username),
            )
            row = connection.execute(
                "SELECT id FROM users WHERE username=? COLLATE NOCASE", (clean_username,)
            ).fetchone()
            self._audit_with_connection(
                connection,
                user_id=int(row["id"]) if row is not None else None,
                username=clean_username,
                action="user.password_reset",
                resource=clean_username,
                result="ok",
            )

    def export_users(self) -> dict[str, Any]:
        rows = self.users.export_rows()
        users = []
        for row in rows:
            item = dict(row)
            item["enabled"] = bool(item["enabled"])
            users.append(item)
        return {"version": 1, "users": users}

    def import_users(self, payload: dict[str, Any], *, replace: bool = True) -> int:
        if int(payload.get("version") or 1) != 1:
            raise ValueError("Unsupported user export version.")
        raw_users = payload.get("users")
        if not isinstance(raw_users, list):
            raise ValueError("User import must contain a users array.")
        normalized: list[dict[str, Any]] = []
        for item in raw_users:
            if not isinstance(item, dict):
                raise ValueError("Every imported user must be an object.")
            username = _clean_username(str(item.get("username") or ""))
            role = _clean_role(str(item.get("role") or "viewer"))
            password_hash = str(item.get("password_hash") or "").strip()
            password = str(item.get("password") or "")
            if password_hash and not password_hash.startswith("$argon2"):
                raise ValueError("Imported password_hash must use Argon2.")
            if not password_hash and len(password) < 8:
                raise ValueError(f"Imported user '{username}' requires a password of at least 8 characters.")
            enabled_value = item.get("enabled", True)
            if not isinstance(enabled_value, bool):
                raise ValueError(f"Imported user '{username}' enabled must be true or false.")
            normalized.append(
                {
                    "username": username,
                    "display_name": str(item.get("display_name") or ""),
                    "password": password or None,
                    "password_hash": password_hash or None,
                    "role": role,
                    "enabled": enabled_value,
                }
            )

        prospective = {
            str(item["username"]).lower(): {"role": item["role"], "enabled": bool(item["enabled"])}
            for item in self.list_users()
        }
        for item in normalized:
            prospective[item["username"].lower()] = {
                "role": item["role"],
                "enabled": item["enabled"],
            }
        if not any(item["role"] == "admin" and item["enabled"] for item in prospective.values()):
            raise ValueError("Import must leave at least one enabled administrator.")

        imported = 0
        for item in normalized:
            self.create_user(
                **item,
                replace=replace,
            )
            imported += 1
        return imported

    def _principal_from_row(self, row: sqlite3.Row) -> AuthPrincipal:
        return AuthPrincipal(
            user_id=int(row["id"]),
            username=str(row["username"]),
            display_name=str(row["display_name"] or ""),
            role=str(row["role"]),
            enabled=bool(row["enabled"]),
        )

    def _audit_with_connection(
        self,
        connection: sqlite3.Connection,
        *,
        username: str,
        action: str,
        result: str,
        user_id: int | None = None,
        resource: str = "",
        detail: str = "",
    ) -> None:
        connection.execute(
            """INSERT INTO audit_logs(user_id, username, action, resource, result, detail, created_at)
               VALUES(?, ?, ?, ?, ?, ?, ?)""",
            (user_id, username, action, resource, result, detail[:2000], int(time.time())),
        )


def auth_database_path(default_path: Path) -> Path:
    return Path(os.getenv("EVSUI_DATABASE_PATH", str(default_path))).expanduser()


def load_user_import(path: Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("User import file must contain a JSON object.")
    return payload
