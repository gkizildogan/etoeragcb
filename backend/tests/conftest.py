from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.config import Settings


@pytest.fixture
def settings_values(tmp_path: Path) -> dict[str, Any]:
    return {
        "app_env": "test",
        "tls_mode": "public",
        "public_domain": "rag.example.com",
        "acme_email": "admin@example.com",
        "allowed_origins": ["https://rag.example.com"],
        "trusted_proxy_ips": ["172.30.0.2"],
        "database_url": "postgresql+asyncpg://rag:password@postgres:5432/rag",
        "redis_url": "redis://redis:6379/0",
        "qdrant_url": "http://qdrant:6333",
        "vllm_base_url": "http://vllm:8000",
        "vllm_model": "cyankiwi/Qwen3.5-9B-AWQ-4bit",
        "vllm_model_revision": "73536aa464f9a93c550aa5a916f0113a08b2f384",
        "max_model_len": 8000,
        "max_new_tokens": 1000,
        "max_generation_concurrency": 1,
        "embed_url": "http://tei-embed:80",
        "embed_model": "BAAI/bge-m3",
        "embed_revision": "5617a9f61b028005a4858fdac845db406aefb181",
        "embed_dim": 1024,
        "rerank_url": "http://tei-rerank:80",
        "rerank_model": "BAAI/bge-reranker-v2-m3",
        "rerank_revision": "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e",
        "jwt_secret": "j" * 48,
        "access_token_ttl": 900,
        "refresh_token_ttl": 2_592_000,
        "signing_secret": "s" * 48,
        "signed_url_ttl": 300,
        "chunk_tokens": 600,
        "chunk_overlap": 80,
        "section_chunk_limit": 4,
        "section_neighbor_radius": 1,
        "retrieve_dense_n": 40,
        "retrieve_sparse_n": 40,
        "rerank_pool_n": 50,
        "rerank_keep": 12,
        "history_turns": 6,
        "history_token_budget": 1200,
        "context_token_budget": 5000,
        "web_top_results": 5,
        "web_fetch_timeout": 8,
        "web_max_bytes": 2_000_000,
        "web_allowed_ports": [80, 443],
        "upload_max_mb": 50,
        "allowed_mime": ["application/pdf", "text/plain"],
        "idempotency_ttl": 86_400,
        "login_rate_limits": ["5/60", "20/3600"],
        "chat_rate_limits": ["10/60", "100/3600"],
        "cache_plan_ttl": 300,
        "cache_retrieval_ttl": 300,
        "cache_rerank_ttl": 300,
        "cache_answer_ttl": 0,
        "backup_destination": "s3://private-backups/rag",
        "backup_encryption_key_file": tmp_path / "backup.agekey",
        "backup_retention": 30,
    }


@pytest.fixture
def settings(settings_values: dict[str, Any]) -> Settings:
    return Settings(**settings_values)
