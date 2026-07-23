from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.rag.candidates import RerankedEvidence, stable_reranked_key


class GateConfigError(RuntimeError):
    pass


class DatasetProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str | None = None
    version: str | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    examples: int = Field(default=0, ge=0)

    @property
    def complete(self) -> bool:
        return (
            self.name is not None
            and self.version is not None
            and self.sha256 is not None
            and self.examples > 0
        )


class ModelProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    embedding_model: str
    embedding_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    reranker_model: str
    reranker_revision: str = Field(pattern=r"^[0-9a-f]{40}$")


class GateThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    top_score_min: float = Field(ge=0.0, le=1.0)
    score_margin_min: float = Field(ge=0.0, le=1.0)
    exact_score_min: float = Field(ge=0.0, le=1.0)
    min_evidence: int = Field(ge=1, le=100)


class GateConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    artifact_id: str = Field(min_length=1, max_length=120)
    calibrated: bool
    dataset: DatasetProvenance
    models: ModelProvenance
    thresholds: GateThresholds | None = None

    @model_validator(mode="after")
    def validate_calibration(self) -> Self:
        if self.calibrated and (self.thresholds is None or not self.dataset.complete):
            raise ValueError("calibrated gate requires thresholds and complete dataset provenance")
        if not self.calibrated and self.thresholds is not None:
            raise ValueError("uncalibrated gate cannot contain thresholds")
        return self


class GateArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    configuration: GateConfiguration
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class GateScores(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    top_score: float | None = Field(default=None, ge=0.0, le=1.0)
    second_score: float | None = Field(default=None, ge=0.0, le=1.0)
    score_margin: float | None = Field(default=None, ge=0.0, le=1.0)
    best_exact_score: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence_count: int = Field(ge=0)


GateReason = Literal[
    "thresholds_passed",
    "exact_threshold_passed",
    "no_candidates",
    "calibration_unavailable",
    "model_revision_mismatch",
    "insufficient_evidence",
    "top_score_below_threshold",
    "score_margin_below_threshold",
]


class GateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    route: Literal["answer", "no_answer"]
    reasons: tuple[GateReason, ...]
    scores: GateScores
    artifact_id: str
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    calibrated: bool
    dataset_name: str | None
    dataset_version: str | None
    dataset_sha256: str | None


def load_gate_artifact(path: Path) -> GateArtifact:
    try:
        raw = path.read_bytes()
        configuration = GateConfiguration.model_validate_json(raw)
    except (OSError, ValidationError, ValueError) as exc:
        raise GateConfigError(f"cannot load retrieval gate artifact: {path}") from exc
    return GateArtifact(
        configuration=configuration,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


class ConfidenceGate:
    def __init__(
        self,
        artifact: GateArtifact,
        *,
        embedding_model: str,
        embedding_revision: str,
        reranker_model: str,
        reranker_revision: str,
    ) -> None:
        self._artifact = artifact
        models = artifact.configuration.models
        self._models_match = (
            models.embedding_model == embedding_model
            and models.embedding_revision == embedding_revision
            and models.reranker_model == reranker_model
            and models.reranker_revision == reranker_revision
        )

    def evaluate(self, candidates: tuple[RerankedEvidence, ...]) -> GateDecision:
        ranked = tuple(sorted(candidates, key=stable_reranked_key))
        top_score = ranked[0].rerank_score if ranked else None
        second_score = ranked[1].rerank_score if len(ranked) > 1 else None
        margin = (
            top_score - second_score
            if top_score is not None and second_score is not None
            else top_score
        )
        exact_scores = [item.rerank_score for item in ranked if item.candidate.matched_exact_terms]
        scores = GateScores(
            top_score=top_score,
            second_score=second_score,
            score_margin=margin,
            best_exact_score=max(exact_scores) if exact_scores else None,
            evidence_count=len(ranked),
        )
        if not ranked:
            return self._decision("no_answer", ("no_candidates",), scores)
        config = self._artifact.configuration
        if not config.calibrated:
            return self._decision("no_answer", ("calibration_unavailable",), scores)
        if not self._models_match:
            return self._decision("no_answer", ("model_revision_mismatch",), scores)
        thresholds = config.thresholds
        if thresholds is None:
            return self._decision("no_answer", ("calibration_unavailable",), scores)
        if (
            scores.best_exact_score is not None
            and scores.best_exact_score >= thresholds.exact_score_min
        ):
            return self._decision("answer", ("exact_threshold_passed",), scores)
        failures: list[GateReason] = []
        if scores.evidence_count < thresholds.min_evidence:
            failures.append("insufficient_evidence")
        if scores.top_score is None or scores.top_score < thresholds.top_score_min:
            failures.append("top_score_below_threshold")
        if scores.score_margin is None or scores.score_margin < thresholds.score_margin_min:
            failures.append("score_margin_below_threshold")
        if failures:
            return self._decision("no_answer", tuple(failures), scores)
        return self._decision("answer", ("thresholds_passed",), scores)

    def _decision(
        self,
        route: Literal["answer", "no_answer"],
        reasons: tuple[GateReason, ...],
        scores: GateScores,
    ) -> GateDecision:
        artifact = self._artifact
        config = artifact.configuration
        return GateDecision(
            route=route,
            reasons=reasons,
            scores=scores,
            artifact_id=config.artifact_id,
            artifact_sha256=artifact.sha256,
            calibrated=config.calibrated,
            dataset_name=config.dataset.name,
            dataset_version=config.dataset.version,
            dataset_sha256=config.dataset.sha256,
        )
