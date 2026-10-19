"""Audio handling: decode uploads to 16 kHz mono PCM, keep one growing PCM file per meeting (temporary)."""

from __future__ import annotations

import fcntl
import io
import shutil
import subprocess
import tempfile
import wave
from contextlib import contextmanager
from pathlib import Path

SAMPLE_RATE = 16_000
BYTES_PER_SECOND = SAMPLE_RATE * 2  # s16le mono


class AudioDecodeError(RuntimeError):
    pass


def decode_file(path: Path) -> bytes:
    """Any ffmpeg-readable file -> raw s16le 16 kHz mono PCM."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(path),
         "-vn", "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "pipe:1"],
        capture_output=True, check=False)
    if proc.returncode != 0:
        raise AudioDecodeError(proc.stderr.decode(errors="replace").strip()[-500:])
    return proc.stdout


def decode_bytes(data: bytes, suffix: str = ".bin") -> bytes:
    # Write to a file: mp4/m4a can't be decoded from a pipe (moov atom may be at the end).
    with tempfile.NamedTemporaryFile(suffix=suffix) as f:
        f.write(data)
        f.flush()
        return decode_file(Path(f.name))


def pcm_seconds(pcm: bytes) -> float:
    return len(pcm) / BYTES_PER_SECOND


def pcm_to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def _offset(seconds: float) -> int:
    return int(seconds * SAMPLE_RATE) * 2


class MeetingAudio:
    """Temporary audio store of one meeting. Deleted once the meeting is transcribed."""

    def __init__(self, data_dir: Path, meeting_id: int):
        self.dir = data_dir / "audio" / str(meeting_id)
        self.pcm_path = self.dir / "audio.pcm"

    @contextmanager
    def _lock(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def add_chunk(self, seq: int, data: bytes, suffix: str, next_seq: int) -> int:
        """Store chunk `seq`; append every consecutive chunk from `next_seq` on. Returns the new next_seq.

        A chunk is raw 16 kHz mono s16le (`.pcm`, what the app sends) or a standalone audio file."""
        with self._lock():
            if seq >= next_seq:
                (self.dir / f"chunk-{seq:06d}{suffix}").write_bytes(data)
            while True:
                found = sorted(self.dir.glob(f"chunk-{next_seq:06d}.*"))
                if not found:
                    return next_seq
                pcm = found[0].read_bytes() if found[0].suffix == ".pcm" else decode_file(found[0])
                with open(self.pcm_path, "ab") as out:
                    out.write(pcm)
                for f in found:
                    f.unlink()
                next_seq += 1

    def add_pcm(self, pcm: bytes) -> None:
        with self._lock(), open(self.pcm_path, "ab") as out:
            out.write(pcm)

    def seconds(self) -> float:
        return self.pcm_path.stat().st_size / BYTES_PER_SECOND if self.pcm_path.exists() else 0.0

    def read(self, start: float, end: float | None = None) -> bytes:
        if not self.pcm_path.exists():
            return b""
        with open(self.pcm_path, "rb") as f:
            f.seek(_offset(start))
            return f.read() if end is None else f.read(max(0, _offset(end) - _offset(start)))

    def delete(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)
