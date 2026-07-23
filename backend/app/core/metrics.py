from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


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
