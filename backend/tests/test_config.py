from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_accepts_measured_p0_limits(settings: Settings) -> None:
    assert settings.max_model_len == 8000
    assert settings.embed_dim == 1024
    assert settings.vllm_model == "cyankiwi/Qwen3.5-9B-AWQ-4bit"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("vllm_model", "another/model"),
        ("max_model_len", 8192),
        ("web_allowed_ports", [80, 8080]),
        ("allowed_origins", ["*"]),
        ("backup_destination", "https://drive.google.com/public-folder"),
        ("chunk_overlap", 600),
        ("rerank_keep", 51),
    ],
)
def test_rejects_unsafe_or_unqualified_values(
    settings_values: dict[str, Any], field: str, value: object
) -> None:
    settings_values[field] = value
    with pytest.raises(ValidationError):
        Settings(**settings_values)


def test_reads_secrets_from_files(settings_values: dict[str, Any], tmp_path: Path) -> None:
    database_file = settings_values["backup_encryption_key_file"].parent / "database_url"
    jwt_file = database_file.parent / "jwt_secret"
    signing_file = database_file.parent / "signing_secret"
    database_file.write_text(
        "postgresql+asyncpg://rag:password@postgres:5432/rag\n", encoding="utf-8"
    )
    jwt_file.write_text("j" * 48, encoding="utf-8")
    signing_file.write_text("s" * 48, encoding="utf-8")
    settings_values.update(
        database_url=None,
        database_url_file=database_file,
        jwt_secret=None,
        jwt_secret_file=jwt_file,
        signing_secret=None,
        signing_secret_file=signing_file,
    )
    settings = Settings(**settings_values)
    assert settings.resolved_database_url().startswith("postgresql+asyncpg://")
    assert settings.resolved_jwt_secret().get_secret_value() == "j" * 48


def test_internal_tls_does_not_require_acme_email(settings_values: dict[str, Any]) -> None:
    settings_values.update(
        tls_mode="internal",
        acme_email=None,
        public_domain="goksu-ubuntu.local",
        allowed_origins=["https://goksu-ubuntu.local"],
    )
    assert Settings(**settings_values).acme_email is None


def test_public_tls_requires_acme_email(settings_values: dict[str, Any]) -> None:
    settings_values["acme_email"] = None
    with pytest.raises(ValidationError):
        Settings(**settings_values)
