"""Almacenamiento durable de trabajos.

- Local (PC): la carpeta de datos YA es durable → no hace nada.
- Nube (disco efímero): cada trabajo se respalda en un repo PRIVADO de datasets de Hugging Face
  (100 GB privados gratis en cuenta free, docs hub/storage-limits 2026-09-28). Al arrancar se
  recuperan estados y resultados; el audio original se baja solo cuando hace falta.

Se activa con RAIZ_HF_REPO (p. ej. "usuario/raiz-transcribe-datos") + HF_TOKEN (secreto, permiso write).
Interfaz: backup(jid, files) · restore_index() · fetch(jid, relpath) -> bool.
"""
import logging
import os
import threading
from pathlib import Path

from . import config

log = logging.getLogger("raiz")
REPO = os.environ.get("RAIZ_HF_REPO", "").strip()
_lock = threading.Lock()
# lo que se sube al respaldo (el mp3 normalizado es regenerable y no se respalda)
SKIP = {"audio_normalizado.mp3"}


def enabled() -> bool:
    return bool(REPO and os.environ.get("HF_TOKEN"))


def _api():
    from huggingface_hub import HfApi
    return HfApi(token=os.environ["HF_TOKEN"])


def ensure_repo():
    if enabled():
        _api().create_repo(REPO, repo_type="dataset", private=True, exist_ok=True)


def backup(jid: str, rel_paths: list[str] | None = None):
    """Sube los archivos del trabajo (o solo rel_paths) al repo privado. Bloqueante: el llamador
    decide si lo corre en un hilo. Devuelve True si quedó respaldado."""
    if not enabled():
        return True
    d = config.DATA_DIR / jid
    from huggingface_hub import CommitOperationAdd
    files = [d / r for r in rel_paths] if rel_paths else [p for p in d.rglob("*") if p.is_file()]
    ops = [CommitOperationAdd(path_in_repo=f"jobs/{jid}/{p.relative_to(d).as_posix()}", path_or_fileobj=str(p))
           for p in files if p.exists() and p.name not in SKIP and not p.name.endswith(".tmp")]
    if not ops:
        return True
    with _lock:
        for attempt in range(3):
            try:
                _api().create_commit(REPO, repo_type="dataset", operations=ops, commit_message=f"{jid}")
                return True
            except Exception as e:  # nunca incluye el token
                log.warning("backup %s intento %d falló: %s", jid, attempt + 1, str(e)[:200])
    return False


def backup_async(jid: str, rel_paths: list[str] | None = None):
    if enabled():
        threading.Thread(target=backup, args=(jid, rel_paths), daemon=True).start()


def restore_index():
    """Al arrancar en la nube: trae estados y resultados (no los audios)."""
    if not enabled():
        return
    from huggingface_hub import snapshot_download
    ensure_repo()
    snapshot_download(REPO, repo_type="dataset", token=os.environ["HF_TOKEN"], local_dir=str(config.DATA_DIR / "_hf"),
                      allow_patterns=["jobs/*/estado.json", "jobs/*/transcripcion.json", "jobs/*/crudo_proveedor.json",
                                      "jobs/*/*.txt", "jobs/*/*.md", "jobs/*/*.pdf"])
    src = config.DATA_DIR / "_hf" / "jobs"
    if src.exists():
        for f in src.rglob("*"):
            if f.is_file():
                dst = config.DATA_DIR / f.relative_to(src)
                if not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(f.read_bytes())


def fetch(jid: str, rel: str) -> bool:
    """Trae un archivo del respaldo si no está en el disco local (p. ej. el audio original)."""
    dst = config.DATA_DIR / jid / rel
    if dst.exists():
        return True
    if not enabled():
        return False
    from huggingface_hub import hf_hub_download
    try:
        p = hf_hub_download(REPO, f"jobs/{jid}/{rel}", repo_type="dataset", token=os.environ["HF_TOKEN"],
                            local_dir=str(config.DATA_DIR / "_hf"))
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(Path(p).read_bytes())
        return True
    except Exception as e:
        log.warning("fetch %s/%s falló: %s", jid, rel, str(e)[:200])
        return False
