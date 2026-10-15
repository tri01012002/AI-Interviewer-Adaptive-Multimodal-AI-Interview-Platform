"""Deterministic, evidence-only assessment against an approved campaign rubric."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from pydantic import BaseModel, ConfigDict

from services.candidate_cv_parser import UNTRUSTED_INSTRUCTION_PATTERN


class IncompatibleRubricError(ValueError):
    pass


class CriterionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quote: str
    start_offset: int
    end_offset: int
    matched_term: str


class CriterionAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criterion_id: str
    criterion_title: str
    score: float | None
    evidence: list[CriterionEvidence]
    explanation: str
    uncertainty: str
    review_required: bool = True


class CandidateScreeningResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rubric_version: str
    rubric_snapshot: list[dict[str, Any]]
    result_source: str = "deterministic"
    synthetic: bool = False
    review_required: bool = True
    assessments: list[CriterionAssessment]


_PROTECTED_OR_PROXY_TERMS = (
    "age", "young", "energetic", "gender", "sex", "race", "ethnicity",
    "religion", "marital status", "pregnancy", "disability", "nationality",
    "native speaker", "culture fit", "appearance", "photo", "graduation year",
)


def assess_candidate_cv(
    rubric: list[dict[str, Any]], extracted_text: str
) -> CandidateScreeningResult:
    normalized_snapshot = json.loads(
        json.dumps(rubric, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    )
    rubric_version = hashlib.sha256(
        json.dumps(
            normalized_snapshot,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if not rubric:
        raise IncompatibleRubricError("The approved campaign rubric is empty")

    assessments: list[CriterionAssessment] = []
    seen_ids: set[str] = set()
    for index, criterion in enumerate(normalized_snapshot):
        if not isinstance(criterion, dict):
            raise IncompatibleRubricError("Each rubric criterion must be an object")
        criterion_id = str(
            criterion.get("criterion_id") or criterion.get("id") or ""
        ).strip()
        title = str(
            criterion.get("criterion")
            or criterion.get("competency")
            or criterion.get("title")
            or ""
        ).strip()
        terms = criterion.get("evidence_terms")
        scale = criterion.get("score_scale")
        if not criterion_id or not title or not isinstance(terms, list) or not terms:
            raise IncompatibleRubricError(
                f"Criterion {index + 1} must define id, title, and evidence_terms"
            )
        if criterion_id in seen_ids:
            raise IncompatibleRubricError("Rubric criterion identifiers must be unique")
        seen_ids.add(criterion_id)
        if (
            not isinstance(scale, dict)
            or not all(key in scale for key in ("minimum", "maximum", "score_when_evidenced"))
        ):
            raise IncompatibleRubricError(
                f"Criterion {criterion_id} must define a numeric score_scale"
            )
        try:
            minimum = float(scale["minimum"])
            maximum = float(scale["maximum"])
            score_when_evidenced = float(scale["score_when_evidenced"])
            normalized_terms = [str(term).strip() for term in terms]
        except (TypeError, ValueError) as exc:
            raise IncompatibleRubricError(
                f"Criterion {criterion_id} has an invalid score scale or evidence term"
            ) from exc
        if (
            not all(
                math.isfinite(value)
                for value in (minimum, maximum, score_when_evidenced)
            )
            or not minimum <= score_when_evidenced <= maximum
            or minimum > maximum
            or any(not term or len(term) > 100 for term in normalized_terms)
        ):
            raise IncompatibleRubricError(
                f"Criterion {criterion_id} has an invalid scale or evidence term"
            )
        screening_text = " ".join(
            [title, *normalized_terms, str(criterion.get("description", ""))]
        ).casefold()
        if any(
            re.search(rf"(?<!\w){re.escape(term)}(?!\w)", screening_text)
            for term in _PROTECTED_OR_PROXY_TERMS
        ):
            raise IncompatibleRubricError(
                f"Criterion {criterion_id} includes a protected characteristic or proxy"
            )

        evidence: list[CriterionEvidence] = []
        for line_match in re.finditer(r"[^\r\n]+", extracted_text):
            line = line_match.group()
            if UNTRUSTED_INSTRUCTION_PATTERN.search(line):
                continue
            for term in normalized_terms:
                pattern = re.compile(
                    rf"(?<![\w+#.]){re.escape(term)}(?![\w+#.])",
                    flags=re.IGNORECASE,
                )
                for match in pattern.finditer(line):
                    start = max(
                        line_match.start(),
                        extracted_text.rfind("\n", 0, line_match.start()) + 1,
                    )
                    end = min(line_match.end(), start + 300)
                    evidence.append(
                        CriterionEvidence(
                            quote=extracted_text[start:end].strip()[:300],
                            start_offset=start,
                            end_offset=end,
                            matched_term=match.group(),
                        )
                    )
        if evidence:
            explanation = (
                "The CV contains an exact match for one or more rubric evidence terms; "
                "this indicates evidence to review, not verified proficiency."
            )
            uncertainty = (
                "Keyword matching cannot establish depth, recency, or proficiency; "
                "a human reviewer must verify the evidence."
            )
            score = score_when_evidenced
        else:
            explanation = "No matching evidence term was found in the extracted CV text."
            uncertainty = (
                "Not found in this CV does not mean the candidate lacks this competency."
            )
            score = None
        assessments.append(
            CriterionAssessment(
                criterion_id=criterion_id,
                criterion_title=title,
                score=score,
                evidence=evidence,
                explanation=explanation,
                uncertainty=uncertainty,
                review_required=True,
            )
        )

    return CandidateScreeningResult(
        rubric_version=rubric_version,
        rubric_snapshot=normalized_snapshot,
        assessments=assessments,
    )
