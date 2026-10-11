"""Settings, from the repo-root `.env`; environment variables win.

Only what differs between environments lives here. Every name is also listed in
`.env.example`, with a comment and no secret value.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    database_url: str
    # Pool per process: 10 kept open, up to 30 more at peaks: 40, as many as the
    # threads that run requests (anyio's default), so a running request never waits
    # for a connection. Below Postgres' default of 100. A request that still waits
    # gets a 503 after 10 s instead of hanging.
    db_pool_size: int = 10
    db_max_overflow: int = 30
    db_pool_timeout_s: int = 10
    # Requests inside the app at once, and how long the others wait in line before
    # a 503 (app/admission.py). The limit matches the thread pool and the database
    # pool above; raise all three together, or none.
    admission_limit: int = 40
    admission_wait_s: float = 10
    # Any single query is cancelled after this; a runaway query cannot hold a
    # connection, and with it the pool, forever. Migrations and dumps do not use it.
    db_statement_timeout_ms: int = 30_000

    # `local`, `dev` or `prod`. Only reported (logs, health), never branched on:
    # behaviour that differs in public follows `app_url` instead.
    app_env: str = "local"
    # Set by the image build; the same value must show in health, logs and the UI.
    app_version: str = "dev"
    # Public UI address. Decides "local or public" (`emails.is_local_address`).
    app_url: str = "http://localhost:3010"
    cors_origins: str = "http://localhost:3010"

    # Browsers accept Secure cookies on http://localhost, so this stays on in
    # development; only tests (http://testserver) turn it off.
    session_cookie_secure: bool = True
    # The idle timeout is one working day; the absolute cap does not move with use, so
    # a stolen session dies after it regardless.
    session_idle_hours: int = 8
    session_max_days: int = 30
    password_min_length: int = 12

    # Fernet master key for the provider keys in `api_keys`. Deliberately no default:
    # a shared default would mean every install encrypts with the same key.
    api_keys_encryption_key: str = ""

    # Empty = no email is sent; the would-be email is logged at WARNING instead.
    resend_api_key: str = ""
    # `onboarding@resend.dev` only delivers to the Resend account's own address.
    email_from: str = "Rootfolder Mock <onboarding@resend.dev>"

    # Relative to the repo root locally, absolute on servers (the mounted volume).
    storage_dir: str = "storage"
    max_upload_mb: int = 500
    # Uploads are refused (507) while the storage has less than this free beyond room
    # for one more upload of the maximum size: a full volume would also stop the
    # nightly backups that are written to it.
    upload_min_free_mb: int = 2048

    # Length of the simulated processing job after an upload.
    job_seconds: int = 30
    # 0 disables the periodic cleanup (tests); `just cleanup` runs it by hand.
    cleanup_interval_hours: int = 24
    # The silent phase of the stream test, where a proxy's read timeout would bite.
    stream_silence_s: int = 20

    log_level: str = "INFO"


settings = Settings()
