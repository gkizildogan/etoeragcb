from datetime import UTC, datetime
from pathlib import Path

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from app.operations.status import read_backup_status


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry(auto_describe=True)
        self.requests = Counter(
            "rag_http_requests_total",
            "HTTP requests completed",
            ("method", "route", "status"),
            registry=self.registry,
        )
        self.request_duration = Histogram(
            "rag_http_request_duration_seconds",
            "HTTP request latency",
            ("method", "route"),
            registry=self.registry,
        )
        self.in_progress = Gauge(
            "rag_http_requests_in_progress",
            "HTTP requests currently being served",
            registry=self.registry,
        )
        self.dependency_ready = Gauge(
            "rag_dependency_ready",
            "Whether a required dependency passed its most recent readiness probe",
            ("dependency",),
            registry=self.registry,
        )
        self.auth_events = Counter(
            "rag_auth_events_total",
            "Authentication events without user-identifying labels",
            ("event",),
            registry=self.registry,
        )
        self.web_retrieval = Counter(
            "rag_web_retrieval_total",
            "Web retrieval branch outcomes",
            ("outcome",),
            registry=self.registry,
        )
        self.web_fetch = Counter(
            "rag_web_fetch_total",
            "Web page fetch outcomes by bounded reason",
            ("outcome", "reason"),
            registry=self.registry,
        )
        self.chat_results = Counter(
            "rag_chat_results_total",
            "Persisted chat results by bounded route",
            ("route",),
            registry=self.registry,
        )
        self.chat_stage_duration = Histogram(
            "rag_chat_stage_duration_seconds",
            "Chat stage latency without request or user labels",
            ("stage",),
            registry=self.registry,
        )
        self.citation_repairs = Counter(
            "rag_citation_repairs_total",
            "Generated answers requiring citation repair",
            registry=self.registry,
        )
        self.generation_tokens = Counter(
            "rag_generation_tokens_total",
            "Generation model token usage",
            ("kind",),
            registry=self.registry,
        )
        self.cache_operations = Counter(
            "rag_cache_operations_total",
            "Cache operations by bounded namespace and outcome",
            ("namespace", "operation", "outcome"),
            registry=self.registry,
        )
        self.cache_operation_duration = Histogram(
            "rag_cache_operation_duration_seconds",
            "Cache operation latency by bounded namespace and operation",
            ("namespace", "operation"),
            registry=self.registry,
        )
        self.backup_last_success = Gauge(
            "rag_backup_last_success_timestamp_seconds",
            "Unix timestamp of the latest verified off-machine backup",
            registry=self.registry,
        )
        self.backup_age = Gauge(
            "rag_backup_age_seconds",
            "Age of the latest verified off-machine backup, or -1 when unavailable",
            registry=self.registry,
        )
        self.backup_status = Gauge(
            "rag_backup_status",
            "Whether the latest backup marker records successful encryption, check, and upload",
            registry=self.registry,
        )

    def refresh_backup_status(self, status_file: Path, *, now: datetime | None = None) -> None:
        checked_at = now or datetime.now(UTC)
        status = read_backup_status(status_file)
        if status is None or not status.is_verified_off_machine:
            self.backup_last_success.set(0)
            self.backup_age.set(-1)
            self.backup_status.set(0)
            return
        completed_at = status.completed_at
        self.backup_last_success.set(completed_at.timestamp())
        self.backup_age.set(max(0.0, (checked_at - completed_at).total_seconds()))
        self.backup_status.set(1)
