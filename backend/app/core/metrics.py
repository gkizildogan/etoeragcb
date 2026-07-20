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
