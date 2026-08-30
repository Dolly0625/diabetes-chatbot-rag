"""Production semantic router — OllamaEmbedder + prototype cosine + abstention.

Conceptually identical to ``PrototypeSemanticRouter`` but self-contained:
no import from ``experiments/``.  Embeddings are L2-normalized; per-label
score is max cosine; policy decides UNKNOWN vs top label.  The router never
raises — failures yield UNKNOWN with degraded flag.
"""

from __future__ import annotations

import hashlib
import os
import time
from typing import Mapping, Sequence

import numpy as np

from .config import ROUTE_LABELS, SemanticRouterConfig
from .telemetry import SemanticRouteObservation, hash_text_prefix

# ---------------------------------------------------------------------------
# Prototypes — generic anchors (not copied from eval dataset verbatim to keep
# eval as paraphrase test).  UNKNOWN has no prototypes; low confidence -> UNKNOWN.
# ---------------------------------------------------------------------------
PROTOTYPES: Mapping[str, tuple[str, ...]] = {
    "PURE_EDUCATION": (
        "我想了解糖尿病的知識和一般衛教。",
        "請解釋糖尿病飲食、運動或用藥的基本原則。",
        "這是一個單純的健康資訊問題，請告訴我原因和注意事項。",
        "我想知道疾病、症狀或藥物的一般說明。",
    ),
    "PURE_INTAKE": (
        "我要準備下次看診，請帶我整理看診前資料。",
        "回診前請一步一步詢問我的用藥、過敏和症狀。",
        "請幫我做一份給醫師看的就診摘要。",
        "我想開始看診前的資料整理流程。",
    ),
    "MIXED": (
        "我要準備看診，同時也想知道糖尿病飲食的基本原則。",
        "請整理我的就診資料，另外回答一個健康衛教問題。",
        "我有症狀要記錄，也想了解這個症狀的一般原因。",
        "回診前幫我準備摘要，並解釋藥物或飲食相關知識。",
    ),
    "CORRECTION": (
        "我剛剛說錯了，請把上一筆資料更正。",
        "更正前面的內容，剛才的日期、藥名或症狀不對。",
        "不是剛才那個意思，請修改上一則記錄。",
        "我要收回並修正前一個回答。",
    ),
    "SUBJECT_CHANGE": (
        "換個話題，不要繼續剛才的問題。",
        "先停下前面的主題，我想改問另一件事。",
        "請切換到新的主題，不要沿用前面的內容。",
        "跳過這一題，我想從另一個方向開始。",
    ),
    "CHITCHAT": (
        "你好，嗨，跟你打個招呼。",
        "謝謝你，辛苦了。",
        "你在嗎？我們隨便聊聊。",
        "晚安，再見，今天聊得不錯。",
    ),
    "UNKNOWN": (),
}


# ---------------------------------------------------------------------------
# Embedders
# ---------------------------------------------------------------------------
class OllamaEmbedder:
    """Thin adapter over langchain_ollama.OllamaEmbeddings."""

    def __init__(self, model_name: str, base_url: str) -> None:
        """Initialize Ollama embedder (lazy — no network on __init__).

        Args:
            model_name: Ollama model name without ``ollama/`` prefix.
            base_url: Ollama base URL.
        """
        from langchain_ollama import OllamaEmbeddings  # lazy import

        self.model_name: str = model_name
        self.base_url: str = base_url
        self._client = OllamaEmbeddings(model=model_name, base_url=base_url)

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        """Embed a batch of texts.

        Args:
            texts: documents to embed.

        Returns:
            Array of shape (len(texts), dim).
        """
        return np.asarray(self._client.embed_documents(list(texts)), dtype=np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        """Embed a single query.

        Args:
            text: query text.

        Returns:
            1-D array.
        """
        return np.asarray(self._client.embed_query(text), dtype=np.float32)


class DeterministicFakeEmbedder:
    """Deterministic 64-dim bigram-hash embedder for hermetic tests.

    Must never be presented as bge-m3.  Used when Ollama is unavailable or
    when ``PYTEST_CURRENT_TEST`` is set.
    """

    model_name = "deterministic-fake-harness-only"

    def _one(self, text: str) -> np.ndarray:
        vec = np.zeros(64, dtype=np.float32)
        raw = (text or "").strip()
        grams = [raw[i : i + 2] for i in range(max(0, len(raw) - 1))] or [raw]
        for gram in grams:
            digest = hashlib.sha256(gram.encode("utf-8")).digest()
            for offset in (0, 2, 4, 6):
                index = int.from_bytes(digest[offset : offset + 2], "big") % len(vec)
                vec[index] += 1.0 if digest[offset + 7] % 2 else -1.0
        norm = float(np.linalg.norm(vec))
        return vec / norm if norm else vec

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        """Embed documents deterministically.

        Args:
            texts: texts to embed.

        Returns:
            Array (len(texts), 64).
        """
        return np.vstack([self._one(t) for t in texts])

    def embed_query(self, text: str) -> np.ndarray:
        """Embed single query deterministically.

        Args:
            text: query text.

        Returns:
            1-D 64-dim vector.
        """
        return self._one(text)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _normalize(vectors: np.ndarray) -> np.ndarray:
    """L2-normalize rows.

    Args:
        vectors: array shape (n, dim) or (dim,).

    Returns:
        L2-normalized array.
    """
    values = np.asarray(vectors, dtype=np.float32)
    if values.ndim == 1:
        values = values.reshape(1, -1)
    norms = np.linalg.norm(values, axis=1, keepdims=True)
    return values / np.maximum(norms, 1e-12)


def _is_fake_embedder(embedder: object) -> bool:
    """Return True if embedder is the deterministic fake."""
    return getattr(embedder, "model_name", "") == DeterministicFakeEmbedder.model_name


# ---------------------------------------------------------------------------
# ProductionSemanticRouter
# ---------------------------------------------------------------------------
class ProductionSemanticRouter:
    """Production router: prototype cosine + margin + fallback UNKNOWN.

    Wraps an embedder (Ollama or fake), pre-embeds prototypes, and scores
    queries with L2 cosine + margin.  Never raises — on failure returns
    UNKNOWN observation.

    Attributes:
        config: router thresholds and mode.
        degraded: True when using DeterministicFakeEmbedder.
    """

    def __init__(
        self,
        embedder: object,
        config: SemanticRouterConfig | None = None,
        prototypes: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        """Initialize router and pre-embed prototypes.

        Args:
            embedder: object with ``embed_documents`` and ``embed_query``.
            config: thresholds/mode/policy; defaults to ``from_env()``.
            prototypes: override prototype mapping (for testing).
        """
        self.config: SemanticRouterConfig = config or SemanticRouterConfig.from_env()
        self.embedder = embedder
        self.degraded: bool = _is_fake_embedder(embedder)
        self._prototypes: Mapping[str, Sequence[str]] = prototypes or PROTOTYPES

        # Build prototype vectors eagerly (embedder already injected; no lazy Ollama call here beyond embed_documents)
        # If embedding fails, mark degraded and use empty vectors so route() still works.
        self._prototype_texts: list[str] = []
        self._prototype_labels: list[str] = []
        for label in ROUTE_LABELS:
            for text in self._prototypes.get(label, ()):
                self._prototype_texts.append(text)
                self._prototype_labels.append(label)

        try:
            if self._prototype_texts:
                vectors = self.embedder.embed_documents(self._prototype_texts)  # type: ignore[union-attr]
                self._vectors: np.ndarray | None = _normalize(vectors)
            else:
                self._vectors = None
        except Exception:
            self._vectors = None

        # index per label
        self._by_label: dict[str, np.ndarray] = {}
        if self._vectors is not None:
            for label in ROUTE_LABELS:
                if label == "UNKNOWN":
                    continue
                idxs = [i for i, lab in enumerate(self._prototype_labels) if lab == label]
                if idxs:
                    self._by_label[label] = np.asarray(idxs, dtype=np.int64)

    def _score(self, text: str) -> dict[str, float]:
        """Compute per-label max cosine.

        Args:
            text: query text.

        Returns:
            Dict label -> max cosine. Empty if vectors unavailable.
        """
        if self._vectors is None or not self._by_label:
            return {}
        query = _normalize(self.embedder.embed_query(text))[0]  # type: ignore[union-attr]
        sims = self._vectors @ query
        return {label: float(np.max(sims[idxs])) for label, idxs in self._by_label.items()}

    def route(self, text: str) -> SemanticRouteObservation:
        """Route a single utterance.

        Applies configured policy (cosine/margin/hybrid), falls back to
        UNKNOWN on low confidence or on any error.  Never raises.

        Args:
            text: user utterance.

        Returns:
            SemanticRouteObservation with route, confidence, margin,
            latency_ms, mode, degraded, matched_labels, scores.
        """
        start = time.perf_counter()
        mode = self.config.mode
        text_len = len(text or "")
        text_h8 = hash_text_prefix(text or "")

        try:
            scores = self._score(text or "")
            if not scores:
                raise RuntimeError("no scores available")
            ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
            top_label, top_score = ranked[0]
            second_score = ranked[1][1] if len(ranked) > 1 else -1.0
            margin = top_score - second_score

            policy = self.config.policy
            cos_th = self.config.cosine_threshold
            mar_th = self.config.margin_threshold

            if policy == "cosine":
                accepted = top_score >= cos_th
            elif policy == "margin":
                accepted = margin >= mar_th
            elif policy == "hybrid":
                accepted = (top_score >= cos_th) and (margin >= mar_th)
            else:
                accepted = (top_score >= cos_th) and (margin >= mar_th)

            route = top_label if accepted else "UNKNOWN"
            matched = tuple(label for label, sc in ranked if sc >= cos_th)
            latency_ms = (time.perf_counter() - start) * 1000.0

            return SemanticRouteObservation(
                route=route,
                confidence=float(top_score),
                margin=float(margin),
                latency_ms=latency_ms,
                mode=mode,
                degraded=self.degraded,
                matched_labels=matched,
                scores=dict(scores),
                text_length=text_len,
                text_hash8=text_h8,
            )
        except Exception:
            latency_ms = (time.perf_counter() - start) * 1000.0
            return SemanticRouteObservation(
                route="UNKNOWN",
                confidence=0.0,
                margin=0.0,
                latency_ms=latency_ms,
                mode=mode,
                degraded=True,
                matched_labels=(),
                scores={},
                text_length=text_len,
                text_hash8=text_h8,
            )

    def is_available(self) -> bool:
        """Return True if prototype vectors are ready and embedder seems live."""
        return self._vectors is not None and len(self._by_label) > 0
