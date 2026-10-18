"""Speaker embeddings, clustering (diarization) and voiceprint matching."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Protocol

import numpy as np

from .audio import SAMPLE_RATE

log = logging.getLogger(__name__)


class Embedder(Protocol):
    def embed(self, pcm: bytes) -> np.ndarray | None: ...


class NullEmbedder:
    def embed(self, pcm: bytes) -> np.ndarray | None:
        return None


class EcapaEmbedder:
    """SpeechBrain ECAPA-TDNN (VoxCeleb). Runs on CPU; needs the `voice` extra."""

    MIN_SECONDS = 0.8

    def __init__(self, cache_dir: str):
        from speechbrain.inference.speaker import EncoderClassifier

        self._model = EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb", savedir=cache_dir, run_opts={"device": "cpu"})

    def embed(self, pcm: bytes) -> np.ndarray | None:
        if len(pcm) < self.MIN_SECONDS * SAMPLE_RATE * 2:
            return None
        import torch

        wav = torch.from_numpy(np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0).unsqueeze(0)
        with torch.no_grad():
            vec = self._model.encode_batch(wav).squeeze().cpu().numpy()
        return normalize(vec.astype(np.float32))


@lru_cache
def get_embedder(kind: str, cache_dir: str) -> Embedder:
    if kind == "none":
        return NullEmbedder()
    try:
        return EcapaEmbedder(cache_dir)
    except Exception as e:  # missing extra or model download failure
        log.warning("speaker embeddings unavailable (%s); all speech will be one unlabelled speaker", e)
        return NullEmbedder()


def normalize(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def to_bytes(v: np.ndarray) -> bytes:
    return np.asarray(v, dtype=np.float32).tobytes()


def from_bytes(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float32).copy()


def cluster(embeddings: list[np.ndarray | None], threshold: float, weights: list[float] | None = None
            ) -> list[int | None]:
    """Group segments by speaker. Labels are 0..k-1 in order of first appearance; None = no embedding."""
    idx = [i for i, e in enumerate(embeddings) if e is not None]
    if not idx:
        return [None] * len(embeddings)
    w = np.array([(weights[i] if weights else 1.0) for i in idx], dtype=np.float32)
    x = np.stack([normalize(embeddings[i]) for i in idx])

    # 1) leader clustering
    sums: list[np.ndarray] = []
    assign = np.zeros(len(idx), dtype=int)
    for j, v in enumerate(x):
        if sums:
            cents = np.stack([normalize(s) for s in sums])
            sims = cents @ v
            best = int(np.argmax(sims))
            if sims[best] >= threshold:
                sums[best] = sums[best] + v * w[j]
                assign[j] = best
                continue
        sums.append(v * w[j])
        assign[j] = len(sums) - 1

    # 2) merge clusters whose centroids are close
    cents = [normalize(s) for s in sums]
    alive = list(range(len(cents)))
    merged = True
    while merged and len(alive) > 1:
        merged = False
        best_pair, best_sim = None, threshold
        for a_i, a in enumerate(alive):
            for b in alive[a_i + 1:]:
                s = float(cents[a] @ cents[b])
                if s >= best_sim:
                    best_pair, best_sim = (a, b), s
        if best_pair:
            a, b = best_pair
            sums[a] = sums[a] + sums[b]
            cents[a] = normalize(sums[a])
            assign[assign == b] = a
            alive.remove(b)
            merged = True

    # 3) one reassignment pass to the nearest centroid
    final = np.stack([cents[a] for a in alive])
    assign = np.array([alive[int(np.argmax(final @ v))] for v in x])

    order: dict[int, int] = {}
    labels: list[int | None] = [None] * len(embeddings)
    for j, i in enumerate(idx):
        labels[i] = order.setdefault(int(assign[j]), len(order))
    return labels


def centroids(embeddings: list[np.ndarray | None], labels: list[int | None],
              weights: list[float] | None = None) -> dict[int, np.ndarray]:
    out: dict[int, np.ndarray] = {}
    for i, (e, lab) in enumerate(zip(embeddings, labels, strict=True)):
        if e is None or lab is None:
            continue
        out[lab] = out.get(lab, 0) + normalize(e) * (weights[i] if weights else 1.0)
    return {k: normalize(v) for k, v in out.items()}


def match_speakers(cents: dict[str, np.ndarray], prints: dict[int, np.ndarray], threshold: float
                   ) -> dict[str, tuple[int, float]]:
    """One-to-one assignment of speaker labels to people (greedy by similarity)."""
    pairs = sorted(((float(normalize(c) @ normalize(p)), lab, pid)
                    for lab, c in cents.items() for pid, p in prints.items()), reverse=True)
    out: dict[str, tuple[int, float]] = {}
    used: set[int] = set()
    for score, lab, pid in pairs:
        if score < threshold:
            break
        if lab in out or pid in used:
            continue
        out[lab] = (pid, round(score, 4))
        used.add(pid)
    return out


def merge_voiceprint(old: np.ndarray | None, samples: int, new: np.ndarray) -> tuple[np.ndarray, int]:
    """Running mean of a person's normalized meeting centroids (capped weight so it keeps adapting)."""
    if old is None:
        return normalize(new), 1
    n = min(samples, 20)
    return normalize(normalize(old) * n + normalize(new)), samples + 1
