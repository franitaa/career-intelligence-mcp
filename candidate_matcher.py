"""Deterministic candidate-evidence retrieval for structured job postings."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

REQUIREMENT_SOURCES = (
    ("required_qualifications", "required_qualifications"),
    ("preferred_qualifications", "preferred_qualifications"),
    ("responsibilities", "responsibilities"),
    ("skills", "skills"),
)
JOB_FIELDS = {
    "url",
    "title",
    "company",
    "location",
    "employment_type",
    "description",
    "responsibilities",
    "required_qualifications",
    "preferred_qualifications",
    "skills",
}
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in",
    "of", "on", "or", "the", "to", "with", "will", "you", "your", "experience",
}


class CandidateMatchError(Exception):
    """An expected, safe input or candidate-profile error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _normalize(value: str) -> str:
    """Normalize comparison text without changing returned source evidence."""
    return re.sub(r"[^a-z0-9+#/&-]+", " ", value.lower()).strip()


def _keywords(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9][a-z0-9+#./&-]*", _normalize(value))
        if token not in STOPWORDS and len(token) > 1
    }


def _as_requirement_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CandidateMatchError("invalid_job", f"The job field '{field}' must be a list of strings.")
    return [item.strip() for item in value if item.strip()]


def _validate_job(job: Any) -> Mapping[str, Any]:
    if not isinstance(job, Mapping):
        raise CandidateMatchError("invalid_job", "The job input must be a structured job object.")
    for field in ("url", "title", "company"):
        if job.get(field) is not None and not isinstance(job.get(field), str):
            raise CandidateMatchError("invalid_job", f"The job field '{field}' must be a string or null.")
    for field in ("responsibilities", "required_qualifications", "preferred_qualifications", "skills"):
        _as_requirement_list(job.get(field), field)
    return job


def _validate_profile(profile: Any) -> list[Mapping[str, Any]]:
    if not isinstance(profile, Mapping) or not isinstance(profile.get("evidence"), list):
        raise CandidateMatchError("profile_invalid", "The candidate profile has an invalid structure.")
    evidence_items = profile["evidence"]
    for item in evidence_items:
        if not isinstance(item, Mapping) or not all(isinstance(item.get(field), str) for field in ("id", "text")):
            raise CandidateMatchError("profile_invalid", "The candidate profile has an invalid structure.")
        if not isinstance(item.get("tags"), list) or not all(isinstance(tag, str) for tag in item["tags"]):
            raise CandidateMatchError("profile_invalid", "The candidate profile has an invalid structure.")
    return evidence_items


def _match_strength(requirement: str, evidence: Mapping[str, Any]) -> tuple[str, int]:
    requirement_text = _normalize(requirement)
    requirement_words = _keywords(requirement)
    tag_matches = {
        _normalize(tag)
        for tag in evidence["tags"]
        if _normalize(tag)
        and (
            _normalize(tag) in requirement_words
            or re.search(rf"(?<![a-z0-9]){re.escape(_normalize(tag))}(?![a-z0-9])", requirement_text)
        )
    }
    evidence_words = _keywords(" ".join([evidence["text"], *evidence["tags"]]))
    word_matches = requirement_words & evidence_words
    phrase_matches = {
        phrase for phrase in (_normalize(tag) for tag in evidence["tags"])
        if len(phrase.split()) > 1 and phrase in requirement_text
    }

    score = len(tag_matches) * 3 + len(phrase_matches) * 3 + len(word_matches)
    if phrase_matches or len(tag_matches) >= 2 or score >= 4:
        return "strong", score
    if score >= 1:
        return "partial", score
    return "none", 0


def _match_requirement(requirement: str, source: str, profile: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    matches: list[tuple[str, int, Mapping[str, Any]]] = []
    for evidence in profile:
        match_type, score = _match_strength(requirement, evidence)
        if match_type != "none":
            matches.append((match_type, score, evidence))

    matches.sort(key=lambda match: (-match[1], str(match[2]["id"])))
    strongest = matches[0][0] if matches else "none"
    return {
        "requirement": requirement,
        "source": source,
        "matched_evidence": [
            {"id": evidence["id"], "text": evidence["text"]}
            for _, _, evidence in matches
        ],
        "match_type": strongest,
    }


def match_candidate_to_job_data(job: Any, profile: Any) -> dict[str, Any]:
    """Return traceable candidate evidence grouped by each job requirement."""
    validated_job = _validate_job(job)
    evidence = _validate_profile(profile)
    requirements: list[dict[str, Any]] = []
    gaps: list[dict[str, str]] = []

    for field, source in REQUIREMENT_SOURCES:
        for requirement in _as_requirement_list(validated_job.get(field), field):
            result = _match_requirement(requirement, source, evidence)
            requirements.append(result)
            if result["match_type"] == "none":
                gaps.append({
                    "requirement": requirement,
                    "source": source,
                    "reason": "No explicit evidence found in the candidate profile.",
                })

    return {
        "job": {
            "title": validated_job.get("title"),
            "company": validated_job.get("company"),
            "url": validated_job.get("url"),
        },
        "requirements": requirements,
        "gaps": gaps,
    }
