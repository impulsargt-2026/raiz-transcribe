"""Grabaciones del grabador integrado (capa 2 anti-pérdida).

El iPhone guarda cada fragmento en su IndexedDB (capa 1, fuente de verdad) y, si hay red, lo
sube acá apenas existe. Al DETENER, `finalize` verifica que estén todos los fragmentos (si falta
alguno responde cuáles, el cliente los reenvía desde IndexedDB), arma el archivo final y crea un
trabajo normal (original preservado + respaldo durable). La transcripción es un paso posterior.

Estructura: DATA_DIR/_rec/<rid>/seg<SSS>/<NNNNNN>.part  ·  un "segmento" = una sesión de
MediaRecorder (se abre otra al CONTINUAR tras una interrupción de iOS).
"""
import json
import re
import shutil
import subprocess
import threading
from collections import defaultdict
from pathlib import Path

from . import config, jobs, media

REC = config.DATA_DIR / "_rec"
_ID = re.compile(r"[A-Za-z0-9_\-]{6,64}")
_locks: defaultdict = defaultdict(threading.Lock)  # un finalize por grabación a la vez (sin duplicados)


def rec_dir(rid: str) -> Path:
    if not _ID.fullmatch(rid):
        raise ValueError("id de grabación inválido")
    return REC / rid


def save_chunk(rid: str, seg: int, n: int, data: bytes):
    if not (0 <= seg < 1000 and 0 <= n < 1_000_000):
        raise ValueError("índice inválido")
    d = rec_dir(rid) / f"seg{seg:03d}"
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"{n:06d}.tmp"
    tmp.write_bytes(data)
    tmp.replace(d / f"{n:06d}.part")


def have(rid: str) -> dict:
    d = rec_dir(rid)
    out = {}
    if d.exists():
        for s in sorted(d.glob("seg*")):
            out[int(s.name[3:])] = sorted(int(p.stem) for p in s.glob("*.part"))
    return out


def _ext(mime: str) -> str:
    return ".webm" if "webm" in (mime or "") else ".mp4"


def _ff(*args) -> bool:
    r = subprocess.run([media.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args],
                       capture_output=True, creationflags=media._NOWIN)
    return r.returncode == 0


def finalize(rid: str, name: str, mime: str, segments: dict) -> dict:
    with _locks[rid]:
        return _finalize(rid, name, mime, segments)


def _finalize(rid: str, name: str, mime: str, segments: dict) -> dict:
    """segments: {"0": cantidad_de_fragmentos, "1": ...}. Devuelve {"missing": [[seg, n], ...]} o el trabajo creado."""
    d = rec_dir(rid)
    meta_p = d / "meta.json"
    if meta_p.exists():  # idempotente: ya finalizada
        m = json.loads(meta_p.read_text(encoding="utf-8"))
        if m.get("job"):
            return {"job": jobs.read_state(m["job"])}
    got = have(rid)
    missing = [[int(s), n] for s, cnt in segments.items() for n in range(int(cnt)) if n not in got.get(int(s), [])]
    if missing:
        return {"missing": missing}
    ext = _ext(mime)
    work = d / "_work"
    work.mkdir(exist_ok=True)
    seg_files = []
    for s in sorted(int(k) for k in segments):
        raw = work / f"seg{s:03d}{ext}"
        with raw.open("wb") as out:
            for n in range(int(segments[str(s)])):
                out.write((d / f"seg{s:03d}" / f"{n:06d}.part").read_bytes())
        seg_files.append(raw)
    final = work / "final.m4a"
    ok = False
    if ext == ".mp4":  # Safari: AAC → solo re-empaquetar, sin recodificar (rápido, sin pérdida)
        if len(seg_files) == 1:
            ok = _ff("-i", str(seg_files[0]), "-vn", "-c:a", "copy", "-movflags", "+faststart", str(final))
        else:
            lst = work / "lista.txt"
            lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in seg_files), encoding="utf-8")
            ok = _ff("-f", "concat", "-safe", "0", "-i", str(lst), "-vn", "-c:a", "copy",
                     "-movflags", "+faststart", str(final))
    if not ok:  # webm/opus (Chrome/PC) o copia fallida → recodificar a AAC mono 64 kbps
        args = []
        for p in seg_files:
            args += ["-i", str(p)]
        filt = "".join(f"[{i}:a]" for i in range(len(seg_files))) + f"concat=n={len(seg_files)}:v=0:a=1[a]"
        ok = _ff(*args, "-filter_complex", filt, "-map", "[a]", "-ac", "1", "-c:a", "aac", "-b:a", "64k",
                 "-movflags", "+faststart", str(final))
    if not ok:
        if len(seg_files) > 1:  # no se borra nada: fragmentos siguen acá y en el iPhone
            raise RuntimeError("No se pudieron unir las partes de la grabación. El audio sigue guardado en el iPhone.")
        final = seg_files[0]  # último recurso: conservar los bytes tal cual
        name = Path(name).stem + ext
    elif not name.lower().endswith(".m4a"):
        name = Path(name).stem + ".m4a"
    st = jobs.create_from_path(final, name, origen="grabacion")
    jobs.write_state(st["id"], grabacion={"rid": rid, "segmentos": len(seg_files),
                                          "fragmentos": sum(int(v) for v in segments.values()), "mime": mime})
    meta_p.write_text(json.dumps({"job": st["id"]}), encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)
    for s in d.glob("seg*"):  # el audio final ya es el original del trabajo (y el iPhone conserva su copia)
        shutil.rmtree(s, ignore_errors=True)
    return {"job": jobs.read_state(st["id"])}
