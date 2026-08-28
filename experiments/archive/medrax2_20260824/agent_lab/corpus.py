from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .schemas import CandidateEvidence


EXPECTED_SOURCE = "TFDA 藥品安全資訊風險溝通資料"


def default_corpus_path() -> Path:
    experiment_root = Path(__file__).resolve().parents[1]
    workspace_root = experiment_root.parent
    return workspace_root / "tfda_context_gate" / "data" / "processed" / "langchain_documents.json"


def _terms(text: str) -> List[str]:
    normalized = text.lower()
    terms = set(re.findall(r"[a-z0-9][a-z0-9+_.-]{1,}", normalized))
    for sequence in re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
        if len(sequence) <= 12:
            terms.add(sequence)
        terms.update(sequence[index : index + 2] for index in range(len(sequence) - 1))
    return sorted(term for term in terms if term.strip())


def _latin_anchors(text: str) -> List[str]:
    # Keep identifiers and unusually specific terms, not every English word.
    # Otherwise an English question such as "What are SGLT2 risks?" would
    # incorrectly require the source document to contain "what" and "are".
    generic = {
        "tfda",
        "fda",
        "drug",
        "drugs",
        "risk",
        "risks",
        "info",
        "information",
        "medicine",
        "medication",
        "diabetes",
        "diabetic",
    }
    return [
        term
        for term in re.findall(r"[a-z][a-z0-9+_.-]{2,}", text.lower())
        if term not in generic and (any(character.isdigit() for character in term) or len(term) >= 10)
    ]


class TFDACorpus:
    """Small read-only corpus adapter used only by the experiment."""

    def __init__(self, path: Optional[Path] = None, rows: Optional[Sequence[Dict[str, Any]]] = None):
        self.path = Path(path) if path is not None else default_corpus_path()
        loaded = list(rows) if rows is not None else self._load(self.path)
        self._rows = self._validate(loaded)
        self._by_id = {str(row["id"]): row for row in self._rows}

    @staticmethod
    def _load(path: Path) -> List[Dict[str, Any]]:
        if not path.is_file():
            raise FileNotFoundError("TFDA corpus not found: %s" % path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("TFDA corpus must be a JSON list")
        return payload

    @staticmethod
    def _validate(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        validated = []
        seen = set()
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError("corpus row %d is not an object" % index)
            evidence_id = row.get("id")
            content = row.get("page_content")
            metadata = row.get("metadata")
            if not evidence_id or not isinstance(content, str) or not content.strip():
                raise ValueError("invalid corpus row %d" % index)
            if not isinstance(metadata, dict):
                raise ValueError("invalid metadata at row %d" % index)
            if evidence_id in seen:
                raise ValueError("duplicate evidence id: %s" % evidence_id)
            seen.add(evidence_id)
            validated.append(row)
        return validated

    def get(self, evidence_id: str) -> Optional[Dict[str, Any]]:
        return self._by_id.get(evidence_id)

    def search(self, query: str, top_k: int = 5, ingredient_only: bool = False) -> List[CandidateEvidence]:
        query_terms = _terms(query)
        anchors = _latin_anchors(query)
        normalized_query = query.lower().strip()
        ranked: List[Tuple[float, Dict[str, Any]]] = []
        for row in self._rows:
            metadata = row["metadata"]
            ingredient = str(metadata.get("藥品成分") or "")
            haystack = "%s\n%s" % (ingredient, row["page_content"])
            normalized_haystack = haystack.lower()
            if anchors and not all(anchor in normalized_haystack for anchor in anchors):
                continue
            if ingredient_only and normalized_query not in ingredient.lower():
                continue
            score = 0.0
            if normalized_query and normalized_query in normalized_haystack:
                score += 8.0
            if normalized_query and normalized_query in ingredient.lower():
                score += 12.0
            for term in query_terms:
                if term in ingredient.lower():
                    score += 3.0
                elif term in normalized_haystack:
                    score += 1.0
            if score > 0:
                ranked.append((score, row))
        ranked.sort(key=lambda item: (-item[0], str(item[1]["id"])))
        return [self._to_evidence(row, score) for score, row in ranked[:top_k]]

    def evidence(self, evidence_ids: Sequence[str]) -> List[CandidateEvidence]:
        results = []
        for evidence_id in evidence_ids:
            row = self.get(evidence_id)
            if row is not None:
                results.append(self._to_evidence(row, 1.0))
        return results

    @staticmethod
    def _to_evidence(row: Dict[str, Any], score: float) -> CandidateEvidence:
        metadata = dict(row["metadata"])
        return CandidateEvidence(
            evidence_id=str(row["id"]),
            content=row["page_content"],
            source=str(metadata.get("source_dataset") or "UNKNOWN"),
            ingredient=str(metadata.get("藥品成分")) if metadata.get("藥品成分") else None,
            published_date=str(metadata.get("發布日期")) if metadata.get("發布日期") else None,
            score=float(score),
            metadata=metadata,
        )
