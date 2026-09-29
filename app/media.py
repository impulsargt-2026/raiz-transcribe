"""Procesamiento de archivo: duración y normalización con FFmpeg (binario de imageio-ffmpeg)."""
import re
import subprocess
from pathlib import Path

import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
ACCEPTED = {".m4a", ".mp3", ".wav", ".aac", ".mp4", ".mov", ".ogg", ".opus", ".flac",
            ".webm", ".wma", ".amr", ".3gp", ".caf", ".aiff", ".aif", ".mpeg", ".mpga", ".m4v"}
_NOWIN = 0x08000000 if hasattr(subprocess, "CREATE_NO_WINDOW") else 0


def duration_seconds(path: Path) -> float | None:
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", creationflags=_NOWIN)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
    if not m:
        return None
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def has_audio(path: Path) -> bool:
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", creationflags=_NOWIN)
    return "Audio:" in r.stderr


def normalize(src: Path, dst: Path) -> Path:
    """Extrae solo el audio, mono 16 kHz MP3 64 kbps: formato que acepta cualquier proveedor
    y reduce ~10x el tamaño a subir (2 h de reunión ≈ 58 MB). El original no se toca."""
    r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "64k", str(dst)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=_NOWIN)
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError(f"FFmpeg no pudo convertir el archivo: {r.stderr.strip()[-400:]}")
    return dst
