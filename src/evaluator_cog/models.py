"""Shared Pydantic models for evaluator-cog."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Finding(BaseModel):
    """A single evaluation finding to be posted to pipeline_evaluations."""

    rule_id: str = Field(default="", description="Standards rule ID, e.g. 'CD-010'.")
    violation_id: str | None = Field(
        default=None,
        description="Machine-readable violation code, e.g. 'CD-010'. "
        "Absent for success/info findings not tied to a specific rule.",
    )
    dimension: str = Field(
        description="Evaluation dimension, e.g. 'structural_conformance'."
    )
    severity: str = Field(description="One of INFO, WARN, or ERROR.")
    finding: str = Field(description="Human-readable finding text.")
    suggestion: str | None = Field(
        default=None, description="Actionable remediation suggestion."
    )


class ConformanceResult(BaseModel):
    """Summary result from a single repo conformance check."""

    repo_id: str = Field(description="Ecosystem repo ID.")
    standards_version: str = Field(description="Standards version evaluated against.")
    findings: list[Finding] = Field(default_factory=list)
    deterministic_count: int = Field(
        default=0, description="Number of deterministic findings."
    )
    llm_count: int = Field(default=0, description="Number of LLM findings.")
