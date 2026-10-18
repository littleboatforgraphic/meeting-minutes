"""Voice activity detection: split the audio stream into speech segments at pauses.

Ilmu ASR returns no timestamps or speakers, so each pause-delimited segment is transcribed on its own and
gets its own speaker embedding."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .audio import SAMPLE_RATE

FRAME_MS = 30
FRAME_BYTES = SAMPLE_RATE * FRAME_MS // 1000 * 2
FRAME_S = FRAME_MS / 1000


@dataclass(frozen=True)
class Segment:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def speech_frames(pcm: bytes, vad: str = "webrtc", aggressiveness: int = 2, energy_threshold: float = 500.0) -> list[bool]:
    n = len(pcm) // FRAME_BYTES
    frames = [pcm[i * FRAME_BYTES:(i + 1) * FRAME_BYTES] for i in range(n)]
    if vad == "energy":
        out = []
        for fr in frames:
            x = np.frombuffer(fr, dtype=np.int16).astype(np.float32)
            out.append(float(np.sqrt(np.mean(x * x))) >= energy_threshold)
        return out
    import webrtcvad

    v = webrtcvad.Vad(aggressiveness)
    return [v.is_speech(fr, SAMPLE_RATE) for fr in frames]


def find_segments(pcm: bytes, offset: float, *, final: bool, vad: str = "webrtc", min_silence: float = 0.5,
                  max_len: float = 30.0, min_speech: float = 0.3, pad: float = 0.15) -> tuple[list[Segment], float]:
    """Segments in `pcm` (which starts at `offset` seconds). Returns (closed segments, consumed_until).

    When not `final`, speech still running at the end of the buffer is left for the next call:
    consumed_until is where the next call must start reading."""
    flags = speech_frames(pcm, vad)
    total = len(flags) * FRAME_S
    silence_needed = max(1, round(min_silence / FRAME_S))
    segs: list[Segment] = []
    start_i: int | None = None
    last_speech_i = -1
    silence = 0

    def close(end_i: int) -> None:
        s = max(0.0, start_i * FRAME_S - pad)
        e = min(total, (end_i + 1) * FRAME_S + pad)
        if (end_i - start_i + 1) * FRAME_S >= min_speech:
            segs.append(Segment(round(offset + s, 3), round(offset + e, 3)))

    for i, is_speech in enumerate(flags):
        if is_speech:
            if start_i is None:
                start_i = i
            last_speech_i = i
            silence = 0
            if (i - start_i + 1) * FRAME_S >= max_len:
                close(i)
                start_i = None
        elif start_i is not None:
            silence += 1
            if silence >= silence_needed:
                close(last_speech_i)
                start_i = None

    if start_i is not None:
        if final:
            close(last_speech_i)
            consumed = total
        else:
            consumed = max(0.0, start_i * FRAME_S - pad)
    else:
        consumed = total
    if final:
        consumed = len(pcm) / (SAMPLE_RATE * 2)
    # Segments must not overlap the region the next call will read.
    segs = [Segment(s.start, min(s.end, offset + consumed)) if not final else s for s in segs]
    return segs, round(offset + consumed, 3)
