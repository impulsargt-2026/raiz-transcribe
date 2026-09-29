"""Trabajos: una carpeta por transcripción con original + crudo + estructurado + TXT/MD/PDF + métricas.
Se procesa de a un audio por vez (cola simple). Nada se borra automáticamente, salvo el AUDIO
ORIGINAL de un trabajo ya transcripto y con más de RETENTION_DAYS (ver _purge_old_originals): la
transcripción (TXT/MD/PDF/JSON) se conserva siempre. Drive es el archivo permanente; RAÍZ es
almacenamiento operativo (29-sep-2026, Cliente Cero)."""
import json
import logging
import os
import queue
import re
import shutil
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from . import config, exporters, media, storage, transcript
from .providers import get_provider

_q: "queue.Queue[str]" = queue.Queue()
_lock = threading.Lock()
# Precio de lista verificado 2026-09-28 (assemblyai.com/pricing): Universal-3.5 Pro 0,21 USD/h + diarización 0,02 USD/h
USD_PER_HOUR = 0.23
RETENTION_DAYS = int(os.environ.get("RAIZ_ORIGINAL_RETENTION_DAYS", "7"))


def job_dir(jid: str) -> Path:
    if not re.fullmatch(r"[\w\-]+", jid):
        raise ValueError("id inválido")
    return config.DATA_DIR / jid


def read_state(jid: str) -> dict:
    return json.loads((job_dir(jid) / "estado.json").read_text(encoding="utf-8"))


def write_state(jid: str, **upd) -> dict:
    with _lock:
        p = job_dir(jid) / "estado.json"
        st = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        st.update(upd)
        p.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        return st


def _safe_name(filename: str) -> str:
    return re.sub(r'[\\/:*?"<>|\r\n]+', "_", Path(filename or "audio").name).strip() or "audio"


def _new_id(name: str) -> str:
    slug = re.sub(r"[^\w\-]+", "-", Path(name).stem, flags=re.UNICODE).strip("-")[:40] or "audio"
    base = f"{datetime.now():%Y%m%d-%H%M%S}_{slug}"
    jid, n = base, 1
    with _lock:
        while (config.DATA_DIR / jid).exists():  # varios archivos en el mismo segundo (lotes)
            n += 1
            jid = f"{base}-{n}"
        (config.DATA_DIR / jid / "original").mkdir(parents=True)
    return jid


def create(filename: str, fileobj, origen: str = "archivo", lote: str | None = None) -> dict:
    safe = _safe_name(filename)
    jid = _new_id(safe)
    orig = job_dir(jid) / "original" / safe
    with orig.open("wb") as f:
        while b := fileobj.read(1 << 20):
            f.write(b)
    return _register(jid, safe, origen, lote)


def create_from_path(src: Path, filename: str, origen: str = "grabacion") -> dict:
    safe = _safe_name(filename)
    jid = _new_id(safe)
    os.replace(src, job_dir(jid) / "original" / safe)
    return _register(jid, safe, origen, None)


def _register(jid: str, safe: str, origen: str, lote: str | None) -> dict:
    orig = job_dir(jid) / "original" / safe
    size = orig.stat().st_size
    base = dict(id=jid, archivo_original=safe, tamano_bytes=size, origen=origen, lote=lote, creado=_now())
    if not media.has_audio(orig):
        write_state(jid, **base, estado="error", etapa="", error="El archivo no contiene una pista de audio reconocible.")
        storage.backup_async(jid)
        return read_state(jid)
    dur = media.duration_seconds(orig)
    st = write_state(jid, **base, duracion=dur, fecha_archivo=None, estado="cargado",
                     etapa="Listo para transcribir", error=None)
    if dur and dur > 10 * 3600:
        st = write_state(jid, estado="error", error="El audio supera 10 h (límite del proveedor).")
    storage.backup_async(jid)  # original + estado respaldados apenas llegan (nube)
    return st


def rename(jid: str, new_name: str) -> dict:
    """Renombra el audio original (conserva la extensión) y regenera TXT/MD/PDF si ya hay transcripción."""
    st = read_state(jid)
    old = st["archivo_original"]
    stem = _safe_name(new_name).strip()
    if not stem:
        return st
    ext = Path(old).suffix
    if Path(stem).suffix.lower() != ext.lower():
        stem = stem + ext
    d = job_dir(jid)
    storage.fetch(jid, f"original/{old}")
    if (d / "original" / old).exists():
        os.replace(d / "original" / old, d / "original" / stem)
    st = write_state(jid, archivo_original=stem)
    if st.get("estado") == "listo":
        doc = load_doc(jid)
        doc["archivo_original"] = stem
        for f in (st.get("archivos") or {}).values():
            if f != "transcripcion.json" and (d / f).exists():
                (d / f).unlink()  # vistas regenerables; el JSON fuente se conserva
        save_doc(jid, doc)
    storage.backup_async(jid)
    return read_state(jid)


def delete_job(jid: str):
    """ELIMINAR: borra el trabajo (carpeta local + respaldo en RAÍZ). No toca la copia en el
    dispositivo (esa vive en el IndexedDB del navegador, capa aparte — ver recorder.js)."""
    storage.delete(jid)
    shutil.rmtree(job_dir(jid), ignore_errors=True)


def _purge_old_originals():
    """Retención de 7 días (RETENTION_DAYS) para el AUDIO ORIGINAL de trabajos ya transcriptos: se
    borra el archivo pesado (local + respaldo) pero la transcripción (TXT/MD/PDF/JSON) queda intacta
    para siempre. Se ejecuta "de paso" en cada list_jobs (que ya se llama en cada carga de la página) —
    sin cron ni infraestructura nueva: alcanza con el tráfico normal de uso para mantenerlo al día."""
    if RETENTION_DAYS <= 0:
        return
    cutoff = time.time() - RETENTION_DAYS * 86400
    done = 0
    for p in config.DATA_DIR.glob("*/estado.json"):
        if done >= 5:  # como corre en el camino de cada carga de página, se limita por llamada
            break
        if p.parent.name.startswith("_"):
            continue
        try:
            st = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if st.get("estado") != "listo" or st.get("original_purgado"):
            continue
        try:
            if datetime.fromisoformat(st["creado"]).timestamp() > cutoff:
                continue
        except Exception:
            continue
        jid, orig = st["id"], (job_dir(st["id"]) / "original" / st["archivo_original"])
        orig.unlink(missing_ok=True)
        try:
            (job_dir(jid) / "original").rmdir()  # solo si quedó vacía
        except OSError:
            pass
        storage.delete_file(jid, f"original/{st['archivo_original']}")
        write_state(jid, original_purgado=True)
        done += 1


def enqueue(jid: str):
    st = read_state(jid)
    if st["estado"] in ("en_cola", "procesando"):
        return st
    st = write_state(jid, estado="en_cola", etapa="En cola", error=None)
    _q.put(jid)
    storage.backup_async(jid, ["estado.json"])
    return st


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _process(jid: str):
    d = job_dir(jid)
    st = read_state(jid)
    t0 = time.time()
    # paso: 1 procesando audio · 2 enviando al proveedor · 3 transcribiendo · 4 generando archivos · 5 listo
    write_state(jid, estado="procesando", etapa="Preparando el audio", paso=1, inicio=st.get("inicio") or _now(),
                paso_desde=_now())
    tid = st.get("transcript_id")
    audio = None
    if not tid:  # si ya se envió antes de un reinicio, no hace falta el audio
        if not storage.fetch(jid, f"original/{st['archivo_original']}"):
            raise RuntimeError("No encuentro el audio original de este trabajo.")
        orig = d / "original" / st["archivo_original"]
        audio = media.normalize(orig, d / "audio_normalizado.mp3") if config.NORMALIZE else orig
    prov = get_provider()

    def on_status(s):
        write_state(jid, etapa=s, paso=3 if s.startswith("Transcrib") else 2, paso_desde=_now())

    def on_submitted(t):
        write_state(jid, transcript_id=t)
        storage.backup_async(jid, ["estado.json"])

    raw, result = prov.transcribe(audio, config.LANGUAGE, on_status=on_status, on_submitted=on_submitted,
                                  transcript_id=tid)
    (d / "crudo_proveedor.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    write_state(jid, etapa="Generando TXT / MD / PDF", paso=4, paso_desde=_now())
    meta = {"id": jid, "archivo_original": st["archivo_original"], "fecha": _now(),
            "fecha_archivo": st.get("fecha_archivo") or "—", "duracion": st.get("duracion"),
            "proveedor": prov.name}
    doc = transcript.build(meta, result)
    elapsed = time.time() - t0
    dur = doc.get("duracion") or st.get("duracion") or 0
    doc["metricas"] = {
        "duracion_audio_s": dur, "tamano_original_bytes": st["tamano_bytes"],
        "tamano_enviado_bytes": audio.stat().st_size if audio else None, "tiempo_procesamiento_s": round(elapsed, 1),
        "ratio_procesamiento_audio": round(elapsed / dur, 3) if dur else None,
        "hablantes": len(doc["speakers"]), "segmentos": len(doc["segments"]),
        "palabras": sum(len(s["words"]) for s in doc["segments"]),
        "costo_estimado_usd": round(dur / 3600 * USD_PER_HOUR, 4),
        "nota_costo": "Precio de lista; se descuenta de los 50 USD de crédito gratuito de AssemblyAI.",
    }
    save_doc(jid, doc)
    write_state(jid, estado="listo", etapa="Listo", paso=5, metricas=doc["metricas"], fin=_now())
    storage.backup_async(jid)


def save_doc(jid: str, doc: dict):
    d = job_dir(jid)
    (d / "transcripcion.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    stem = f"{Path(doc['archivo_original']).stem} - transcripcion"
    files = exporters.write_all(doc, d, stem)
    files["json"] = "transcripcion.json"
    write_state(jid, archivos=files)


def load_doc(jid: str) -> dict:
    return json.loads((job_dir(jid) / "transcripcion.json").read_text(encoding="utf-8"))


def _worker():
    while True:
        jid = _q.get()
        try:
            _process(jid)
        except Exception as e:  # el mensaje nunca contiene la API key (ver providers)
            (job_dir(jid) / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
            # el audio NUNCA se borra: el trabajo queda en error, reintentable
            # si AssemblyAI ya transcribió y falló algo posterior (p. ej. el PDF), se conserva el id: REINTENTAR solo
            # vuelve a consultar el resultado (gratis) en vez de subir y cobrar de nuevo
            keep = read_state(jid).get("transcript_id") if not str(e).startswith("AssemblyAI devolvió error") else None
            write_state(jid, estado="error", etapa="", error=str(e)[:500], transcript_id=keep)
            storage.backup_async(jid, ["estado.json", "error.log"])


def start_worker():
    try:
        storage.restore_index()  # nube: recupera trabajos del respaldo al arrancar
    except Exception as e:
        (config.DATA_DIR / "restore_error.log").write_text(str(e)[:500], encoding="utf-8")
    # trabajos que quedaron a medias por un reinicio vuelven a la cola (con su transcript_id si ya se enviaron)
    for p in sorted(p for p in config.DATA_DIR.glob("*/estado.json") if not p.parent.name.startswith("_")):
        st = json.loads(p.read_text(encoding="utf-8"))
        if st.get("estado") in ("en_cola", "procesando"):
            write_state(st["id"], estado="en_cola", etapa="En cola (reanudado)")
            _q.put(st["id"])
    threading.Thread(target=_worker, daemon=True).start()


def list_jobs(limit=30, ids: list[str] | None = None) -> list:
    try:
        _purge_old_originals()
    except Exception as e:  # nunca rompe el listado por un fallo de purga
        logging.getLogger("raiz").warning("purga de originales falló: %s", str(e)[:200])
    out = []
    paths = [job_dir(i) / "estado.json" for i in ids] if ids else \
        sorted((p for p in config.DATA_DIR.glob("*/estado.json") if not p.parent.name.startswith("_")), reverse=True)[:limit]
    for p in paths:
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            pass
    return out
