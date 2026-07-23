from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

QueryCategory = Literal[
    "exact_id",
    "heading",
    "collection",
    "semantic",
    "repeated_passage",
    "scoped",
    "web_document",
    "ambiguous_hint",
    "unanswerable",
]
ModeName = Literal[
    "sparse_only",
    "dense_only",
    "hybrid",
    "scoped_hybrid",
    "reranked_hybrid",
]


class CorpusRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[cw][0-9]{3}$")
    source_type: Literal["document", "web"]
    document_id: str = Field(min_length=1, max_length=120)
    document_title: str = Field(min_length=1, max_length=300)
    source_filename: str | None = Field(default=None, max_length=300)
    collection_ids: tuple[str, ...] = ()
    heading: str = Field(min_length=1, max_length=300)
    page: int = Field(ge=1)
    text: str = Field(min_length=20, max_length=10_000)
    uri: HttpUrl | None = None
    domain: str | None = Field(default=None, max_length=253)
    duplicate_group: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        if self.source_type == "web" and (self.uri is None or self.domain is None):
            raise ValueError("web records require uri and domain")
        if self.source_type == "document" and self.source_filename is None:
            raise ValueError("document records require source_filename")
        return self


class QueryScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_ids: tuple[str, ...] = ()
    collection_ids: tuple[str, ...] = ()
    headings: tuple[str, ...] = ()


class GoldenQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^q[0-9]{3}$")
    language: Literal["en", "tr"]
    category: QueryCategory
    query: str = Field(min_length=3, max_length=1_000)
    answerable: bool
    relevance: dict[str, int] = Field(default_factory=dict)
    exact_terms: tuple[str, ...] = ()
    scope: QueryScope = Field(default_factory=QueryScope)
    boost_document_ids: tuple[str, ...] = ()
    web_search: bool = False

    @model_validator(mode="after")
    def validate_labels(self) -> Self:
        if self.answerable != bool(self.relevance):
            raise ValueError("answerable queries must have relevance labels and vice versa")
        if any(value < 1 or value > 3 for value in self.relevance.values()):
            raise ValueError("relevance grades must be within 1..3")
        if self.category == "unanswerable" and self.answerable:
            raise ValueError("unanswerable category cannot carry a positive label")
        return self


class RankingTargets(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reranked_recall_at_5_min: float = Field(ge=0.0, le=1.0)
    reranked_mrr_min: float = Field(ge=0.0, le=1.0)
    reranked_ndcg_at_10_min: float = Field(ge=0.0, le=1.0)
    scoped_recall_at_5_min: float = Field(ge=0.0, le=1.0)


class GateTargets(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    precision_min: float = Field(ge=0.0, le=1.0)
    recall_min: float = Field(ge=0.0, le=1.0)


class FileProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    records: int = Field(ge=1)


class GoldenManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    name: str
    version: str
    license: Literal["CC0-1.0"]
    description: str
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    corpus: FileProvenance
    queries: FileProvenance
    ranking_targets: RankingTargets
    gate_targets: GateTargets


class DatasetBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest: GoldenManifest
    corpus: tuple[CorpusRecord, ...]
    queries: tuple[GoldenQuery, ...]


class QueryRanking(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query_id: str
    ranked_ids: tuple[str, ...]
    scores: tuple[float, ...]
    latency_ms: float = Field(ge=0.0)


class RankingMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evaluated_answerable_queries: int = Field(ge=1)
    recall_at_5: float = Field(ge=0.0, le=1.0)
    recall_at_10: float = Field(ge=0.0, le=1.0)
    mrr: float = Field(ge=0.0, le=1.0)
    ndcg_at_10: float = Field(ge=0.0, le=1.0)
    latency_p50_ms: float = Field(ge=0.0)
    latency_p95_ms: float = Field(ge=0.0)
    mean_unique_sources_at_10: float = Field(ge=0.0)
    mean_unique_domains_at_10: float = Field(ge=0.0)
    mean_source_types_at_10: float = Field(ge=0.0)


class ModeEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: ModeName
    metrics: RankingMetrics
    by_language: dict[str, RankingMetrics]
    by_category: dict[str, RankingMetrics]
    queries: tuple[QueryRanking, ...]


class GateObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query_id: str
    expected_answer: bool
    answerable: bool
    relevant_in_context: bool
    top_score: float | None = Field(default=None, ge=0.0, le=1.0)
    second_score: float | None = Field(default=None, ge=0.0, le=1.0)
    score_margin: float | None = Field(default=None, ge=0.0, le=1.0)
    best_exact_score: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence_count: int = Field(ge=0)


class CalibratedThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    top_score_min: float = Field(ge=0.0, le=1.0)
    score_margin_min: float = Field(ge=0.0, le=1.0)
    exact_score_min: float = Field(ge=0.0, le=1.0)
    min_evidence: int = Field(ge=1, le=100)


class ClassificationMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    true_positive: int = Field(ge=0)
    false_positive: int = Field(ge=0)
    true_negative: int = Field(ge=0)
    false_negative: int = Field(ge=0)
    precision: float = Field(ge=0.0, le=1.0)
    recall: float = Field(ge=0.0, le=1.0)
    f1: float = Field(ge=0.0, le=1.0)


class CalibrationEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    thresholds: CalibratedThresholds
    metrics: ClassificationMetrics
    precision_target: float
    recall_target: float
    targets_met: bool
    combinations_evaluated: int = Field(ge=1)
    observations: tuple[GateObservation, ...]


class EvaluationProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_name: str
    dataset_version: str
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    query_examples: int = Field(ge=1)
    corpus_records: int = Field(ge=1)
    evaluator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    embedding_model: str
    embedding_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    reranker_model: str
    reranker_revision: str = Field(pattern=r"^[0-9a-f]{40}$")


class AcceptanceEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool
    checks: dict[str, bool]


class EvaluationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    report_id: str
    provenance: EvaluationProvenance
    configuration: dict[str, int | float]
    modes: tuple[ModeEvaluation, ...]
    calibration: CalibrationEvaluation
    acceptance: AcceptanceEvaluation
