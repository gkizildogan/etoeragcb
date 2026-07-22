from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    AnyHttpUrl,
    EmailStr,
    Field,
    PostgresDsn,
    RedisDsn,
    SecretStr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

EXPECTED_VLLM_MODEL = "cyankiwi/Qwen3.5-9B-AWQ-4bit"
RATE_LIMIT_RE = re.compile(r"^[1-9][0-9]*/[1-9][0-9]*$")


class Settings(BaseSettings):
    """Validated process configuration; no production secret has a default."""

    model_config = SettingsConfigDict(
        env_file=None,
        env_prefix="",
        extra="forbid",
        case_sensitive=False,
        frozen=True,
    )

    app_env: Literal["development", "test", "production"] = "production"
    tls_mode: Literal["public", "internal"] = "public"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    public_domain: str
    acme_email: EmailStr | None = None
    allowed_origins: list[str]
    internal_hosts: list[str] = Field(default_factory=lambda: ["backend", "localhost", "127.0.0.1"])
    trusted_proxy_ips: list[str]

    database_url: SecretStr | None = None
    database_url_file: Path | None = None
    redis_url: RedisDsn
    qdrant_url: AnyHttpUrl
    qdrant_collection: str = Field(min_length=1, max_length=120)

    vllm_base_url: AnyHttpUrl
    vllm_model: str
    vllm_model_revision: str
    max_model_len: int = Field(ge=2048, le=8000)
    max_new_tokens: int = Field(ge=1, le=2048)
    max_generation_concurrency: int = Field(ge=1, le=2)

    embed_url: AnyHttpUrl
    embed_model: str
    embed_revision: str
    embed_dim: int = Field(ge=1)
    rerank_url: AnyHttpUrl
    rerank_model: str
    rerank_revision: str

    jwt_secret: SecretStr | None = None
    jwt_secret_file: Path | None = None
    access_token_ttl: int = Field(ge=60, le=3600)
    refresh_token_ttl: int = Field(ge=3600, le=31_536_000)
    signing_secret: SecretStr | None = None
    signing_secret_file: Path | None = None
    signed_url_ttl: int = Field(ge=30, le=3600)

    chunk_tokens: int = Field(ge=64, le=2048)
    chunk_overlap: int = Field(ge=0, le=512)
    section_chunk_limit: int = Field(ge=1, le=20)
    document_chunk_limit: int = Field(ge=1, le=50)
    domain_chunk_limit: int = Field(ge=1, le=20)
    section_neighbor_radius: int = Field(ge=0, le=5)
    retrieve_dense_n: int = Field(ge=1, le=500)
    retrieve_sparse_n: int = Field(ge=1, le=500)
    rerank_pool_n: int = Field(ge=1, le=200)
    rerank_keep: int = Field(ge=1, le=100)
    history_turns: int = Field(ge=0, le=50)
    history_token_budget: int = Field(ge=0, le=4000)
    context_token_budget: int = Field(ge=256, le=7000)
    retrieval_gate_config: Path

    web_top_results: int = Field(ge=1, le=20)
    web_fetch_timeout: float = Field(gt=0, le=60)
    web_max_bytes: int = Field(ge=1024, le=20_000_000)
    web_allowed_ports: list[int]

    upload_max_mb: int = Field(ge=1, le=500)
    tenant_upload_quota_mb: int = Field(ge=1, le=1_000_000)
    allowed_mime: list[str]
    document_storage_root: Path
    ingest_batch_size: int = Field(ge=1, le=64)
    ingestion_heartbeat_timeout: int = Field(ge=30, le=3600)
    retained_index_generations: int = Field(ge=2, le=100)
    idempotency_ttl: int = Field(ge=60, le=604_800)
    login_rate_limits: list[str]
    chat_rate_limits: list[str]
    upload_rate_limits: list[str]
    cache_plan_ttl: int = Field(ge=0, le=86_400)
    cache_retrieval_ttl: int = Field(ge=0, le=86_400)
    cache_rerank_ttl: int = Field(ge=0, le=86_400)
    cache_answer_ttl: int = Field(ge=0, le=86_400)

    backup_destination: str
    backup_encryption_key_file: Path
    backup_retention: int = Field(ge=2, le=3650)
    readiness_timeout_seconds: float = Field(default=3.0, gt=0, le=30)

    @field_validator("public_domain")
    @classmethod
    def validate_public_domain(cls, value: str) -> str:
        candidate = value.strip().lower().rstrip(".")
        if (
            not candidate
            or "://" in candidate
            or "/" in candidate
            or candidate == "localhost"
            or "." not in candidate
        ):
            raise ValueError("PUBLIC_DOMAIN must be a DNS hostname without scheme, port, or path")
        return candidate

    @field_validator("acme_email", mode="before")
    @classmethod
    def empty_acme_email_is_none(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("allowed_origins")
    @classmethod
    def validate_origins(cls, origins: list[str]) -> list[str]:
        if not origins:
            raise ValueError("ALLOWED_ORIGINS cannot be empty")
        normalized: list[str] = []
        for origin in origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.path not in {"", "/"}
            ):
                raise ValueError("origins must contain only scheme and authority")
            normalized.append(origin.rstrip("/"))
        if len(normalized) != len(set(normalized)):
            raise ValueError("ALLOWED_ORIGINS cannot contain duplicates")
        return normalized

    @field_validator("web_allowed_ports")
    @classmethod
    def validate_web_ports(cls, ports: list[int]) -> list[int]:
        if not ports or any(port not in {80, 443} for port in ports):
            raise ValueError("WEB_ALLOWED_PORTS must contain only 80 and/or 443 in P1")
        return sorted(set(ports))

    @field_validator("login_rate_limits", "chat_rate_limits", "upload_rate_limits")
    @classmethod
    def validate_rate_limits(cls, limits: list[str]) -> list[str]:
        if not limits or any(RATE_LIMIT_RE.fullmatch(limit) is None for limit in limits):
            raise ValueError("rate limits must use count/window_seconds, for example 5/60")
        return limits

    @field_validator("qdrant_collection")
    @classmethod
    def validate_qdrant_collection(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", value) is None:
            raise ValueError("QDRANT_COLLECTION contains unsupported characters")
        return value

    @field_validator("backup_destination")
    @classmethod
    def validate_backup_destination(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"s3", "sftp", "gdrive"} or not parsed.netloc:
            raise ValueError(
                "BACKUP_DESTINATION must be an authenticated s3://, sftp://, or "
                "gdrive:// locator, not a public sharing URL"
            )
        return value

    @model_validator(mode="after")
    def validate_cross_field_constraints(self) -> Self:
        if self.tls_mode == "public" and self.acme_email is None:
            raise ValueError("ACME_EMAIL is required when TLS_MODE=public")
        if self.vllm_model != EXPECTED_VLLM_MODEL:
            raise ValueError(f"VLLM_MODEL must remain {EXPECTED_VLLM_MODEL}")
        if not re.fullmatch(r"[0-9a-f]{40}", self.vllm_model_revision):
            raise ValueError("VLLM_MODEL_REVISION must be an exact 40-character commit")
        for name, revision in (
            ("EMBED_REVISION", self.embed_revision),
            ("RERANK_REVISION", self.rerank_revision),
        ):
            if not re.fullmatch(r"[0-9a-f]{40}", revision):
                raise ValueError(f"{name} must be an exact 40-character commit")
        if self.chunk_overlap >= self.chunk_tokens:
            raise ValueError("CHUNK_OVERLAP must be smaller than CHUNK_TOKENS")
        if self.rerank_keep > self.rerank_pool_n:
            raise ValueError("RERANK_KEEP cannot exceed RERANK_POOL_N")
        if self.section_chunk_limit > self.document_chunk_limit:
            raise ValueError("SECTION_CHUNK_LIMIT cannot exceed DOCUMENT_CHUNK_LIMIT")
        reserved_tokens = (
            self.max_new_tokens + self.history_token_budget + self.context_token_budget
        )
        if reserved_tokens > self.max_model_len:
            raise ValueError("history + context + output token budgets exceed MAX_MODEL_LEN")
        if f"https://{self.public_domain}" not in self.allowed_origins:
            raise ValueError("ALLOWED_ORIGINS must contain the public HTTPS origin")
        self.resolved_database_url()
        self.resolved_jwt_secret()
        self.resolved_signing_secret()
        return self

    def _read_secret_file(self, path: Path | None, label: str) -> str | None:
        if path is None:
            return None
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(f"cannot read {label}_FILE") from exc
        if not value:
            raise ValueError(f"{label}_FILE is empty")
        return value

    def resolved_database_url(self) -> str:
        direct = self.database_url.get_secret_value() if self.database_url is not None else None
        from_file = self._read_secret_file(self.database_url_file, "DATABASE_URL")
        if (direct is None) == (from_file is None):
            raise ValueError("set exactly one of DATABASE_URL or DATABASE_URL_FILE")
        value = direct or from_file
        assert value is not None
        try:
            parsed = TypeAdapter(PostgresDsn).validate_python(value)
        except ValidationError as exc:
            raise ValueError("DATABASE_URL is not a valid PostgreSQL DSN") from exc
        if parsed.scheme != "postgresql+asyncpg":
            raise ValueError("DATABASE_URL must use postgresql+asyncpg")
        return str(parsed)

    def _resolved_secret(
        self, direct: SecretStr | None, secret_file: Path | None, label: str
    ) -> SecretStr:
        direct_value = direct.get_secret_value() if direct is not None else None
        file_value = self._read_secret_file(secret_file, label)
        if (direct_value is None) == (file_value is None):
            raise ValueError(f"set exactly one of {label} or {label}_FILE")
        value = direct_value or file_value
        assert value is not None
        if len(value) < 32 or value.lower() in {"change-me", "changeme"}:
            raise ValueError(f"{label} must contain at least 32 non-placeholder characters")
        return SecretStr(value)

    def resolved_jwt_secret(self) -> SecretStr:
        return self._resolved_secret(self.jwt_secret, self.jwt_secret_file, "JWT_SECRET")

    def resolved_signing_secret(self) -> SecretStr:
        return self._resolved_secret(
            self.signing_secret, self.signing_secret_file, "SIGNING_SECRET"
        )

    @property
    def allowed_hosts(self) -> list[str]:
        return [self.public_domain, *self.internal_hosts]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
