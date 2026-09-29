"""RAÍZ TRANSCRIBE — servidor local (FastAPI). Interfaz en /, API en /api/*."""
import io
import json
import socket
import tempfile
from datetime import datetime
from pathlib import Path

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
import zipfile

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile as StarletteUploadFile

from . import config, jobs, recordings, storage, transcript

# Log de requests de subida (sin contenido ni claves) para diagnosticar problemas del celular
(config.ROOT_DIR / "logs").mkdir(exist_ok=True)
log = logging.getLogger("raiz")
if not log.handlers:
    _h = logging.FileHandler(config.ROOT_DIR / "logs" / "servidor.log", encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

app = FastAPI(title="RAÍZ TRANSCRIBE", docs_url=None, redoc_url=None, openapi_url=None)
STATIC = Path(__file__).parent / "static"
LOCAL = {"127.0.0.1", "::1", "localhost"}
PUBLIC = {"/manifest.webmanifest", "/icon.svg", "/icon-180.png", "/icon-192.png", "/icon-512.png", "/favicon.ico",
          "/app.js", "/recorder.js", "/sw.js", "/healthz"}  # sin secretos; la clave va en la página (protegida)
# Dirección para el iPhone; la completa run.py al arrancar (nombre mDNS si quedó activo, si no IP LAN)
ACCESS = {"iphone_base": None, "mdns": False}


def is_local(request: Request) -> bool:
    if config.CLOUD:  # en la nube nadie es "la misma PC": siempre se exige la clave
        return False
    proxied = "x-forwarded-for" in request.headers or "cf-connecting-ip" in request.headers
    return bool(request.client and request.client.host in LOCAL and not proxied)


def authorized(request: Request) -> bool:
    return is_local(request) \
        or request.cookies.get("raiz_k") == config.RAIZ_TOKEN \
        or request.query_params.get("k") == config.RAIZ_TOKEN \
        or request.headers.get("x-raiz-key") == config.RAIZ_TOKEN


@app.middleware("http")
async def auth(request: Request, call_next):
    """Desde la misma PC no pide clave. Desde el celular: ?k=CLAVE (queda en cookie, en el
    almacenamiento local de la página y en la URL de inicio del ícono instalado)."""
    if not authorized(request) and request.url.path not in PUBLIC:
        return HTMLResponse("<meta name=viewport content='width=device-width'><h2>RAÍZ TRANSCRIBE</h2>"
                            "<p>Falta la clave de acceso. Escaneá el QR que muestra la PC "
                            "(recuadro «Instalar en iPhone»).</p>", status_code=401)
    resp = await call_next(request)
    if request.query_params.get("k") == config.RAIZ_TOKEN:
        resp.set_cookie("raiz_k", config.RAIZ_TOKEN, max_age=3600 * 24 * 365, httponly=True, samesite="lax")
    return resp


@app.on_event("startup")
def _startup():
    jobs.start_worker()


def iphone_base() -> str:
    return ACCESS["iphone_base"] or f"http://{lan_ip()}:{config.PORT}"


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    # la página solo se entrega a quien ya está autorizado: puede llevar la clave embebida
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    return html.replace("__RAIZ_K__", config.RAIZ_TOKEN).replace("__RAIZ_LOCAL__", "1" if is_local(request) else "0")


@app.get("/manifest.webmanifest")
def manifest(request: Request):
    m = json.loads((STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))
    if authorized(request):  # el ícono instalado abre ya autorizado
        m["start_url"] = f"/?k={config.RAIZ_TOKEN}"
    return JSONResponse(m, media_type="application/manifest+json")


@app.get("/icon.svg")
def icon():
    return FileResponse(STATIC / "icon.svg", media_type="image/svg+xml")


@app.get("/icon-{size}.png")
def icon_png(size: int):
    if size not in (180, 192, 512):
        raise HTTPException(404)
    return FileResponse(STATIC / f"icon-{size}.png", media_type="image/png")


@app.get("/favicon.ico")
def favicon():
    return FileResponse(STATIC / "icon-192.png", media_type="image/png")


@app.get("/{name}.js")
def js(name: str):
    if name == "sw":
        return service_worker()
    if name not in ("app", "recorder"):
        raise HTTPException(404)
    return FileResponse(STATIC / f"{name}.js", media_type="text/javascript; charset=utf-8",
                        headers={"Cache-Control": "no-cache"})


@app.get("/sw.js")
def service_worker():
    return FileResponse(STATIC / "sw.js", media_type="text/javascript; charset=utf-8",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/ping")
def ping():
    return {"ok": True, "cloud": config.CLOUD, "respaldo": storage.enabled()}


# ---------- grabador integrado ----------
@app.post("/api/rec/{rid}/chunk")
async def rec_chunk(rid: str, request: Request, seg: int, n: int):
    data = await request.body()
    if not data:
        raise HTTPException(400, "Fragmento vacío")
    try:
        recordings.save_chunk(rid, seg, n, data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/rec/{rid}/finalize")
async def rec_finalize(rid: str, request: Request):
    """Body: {"name": "Grabación 2026-09-28 22-45.m4a", "mime": "audio/mp4", "segments": {"0": 37}}"""
    b = await request.json()
    try:
        res = await run_in_threadpool(recordings.finalize, rid, b.get("name") or "Grabación.m4a",
                                      b.get("mime") or "", {str(k): int(v) for k, v in (b.get("segments") or {}).items()})
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    if "missing" in res:
        return JSONResponse(res, status_code=409)
    log.info("grabación %s finalizada → %s", rid, res["job"]["id"])
    return res["job"]


@app.post("/api/jobs/{jid}/rename")
async def rename_job(jid: str, request: Request):
    b = await request.json()
    return await run_in_threadpool(jobs.rename, _exists(jid), str(b.get("name") or ""))


@app.get("/api/zip")
def zip_results(ids: str):
    """Descarga todo (TXT/MD/PDF de cada trabajo listo) en un ZIP, cada uno en su carpeta."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for jid in [i for i in ids.split(",") if i][:50]:
            st = jobs.read_state(_exists(jid))
            for kind in ("pdf", "txt", "md"):
                f = (st.get("archivos") or {}).get(kind)
                if f and (jobs.job_dir(jid) / f).exists():
                    z.write(jobs.job_dir(jid) / f, f"{Path(st['archivo_original']).stem}/{f}")
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f"attachment; filename=RAIZ-transcripciones-{datetime.now():%Y%m%d-%H%M}.zip"})


@app.get("/api/acceso")
def acceso(request: Request):
    """Solo desde la PC: dirección del iPhone y del Atajo (llevan la clave)."""
    if not is_local(request):
        raise HTTPException(403)
    cloud, ctok = config.api_key("RAIZ_CLOUD_URL").rstrip("/"), config.api_key("RAIZ_CLOUD_TOKEN")
    if cloud and ctok:  # versión nube desplegada: el iPhone se instala desde ahí (HTTPS, sin PC)
        return {"iphone_url": f"{cloud}/?k={ctok}", "atajo_url": f"{cloud}/api/atajo?k={ctok}",
                "ip_url": "", "mdns": False, "nube": True}
    b = iphone_base()
    return {"iphone_url": f"{b}/?k={config.RAIZ_TOKEN}", "atajo_url": f"{b}/api/atajo?k={config.RAIZ_TOKEN}",
            "ip_url": f"http://{lan_ip()}:{config.PORT}/?k={config.RAIZ_TOKEN}", "mdns": ACCESS["mdns"], "nube": False}


@app.get("/qr.png")
def qr(request: Request, que: str = "app"):
    if not is_local(request):
        raise HTTPException(403)
    import qrcode
    a = acceso(request)
    img = qrcode.make(a["atajo_url"] if que == "atajo" else a["iphone_url"], box_size=7, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(buf.getvalue(), media_type="image/png")


def _req_summary(request: Request) -> str:
    return (f"{request.method} {request.url.path} de {request.client.host if request.client else '?'} · "
            f"content-type={request.headers.get('content-type', '-')[:120]} · "
            f"content-length={request.headers.get('content-length', '-')} · ua={request.headers.get('user-agent', '-')[:140]}")


@app.exception_handler(RequestValidationError)
async def _validation(request: Request, exc: RequestValidationError):
    errs = [{k: (repr(v)[:120] if k == "input" else v) for k, v in e.items() if k in ("loc", "msg", "type", "input")}
            for e in exc.errors()]
    log.warning("422 %s · errores=%s", _req_summary(request), errs)
    return JSONResponse({"detail": errs}, status_code=422)


@app.post("/api/upload")
async def upload(request: Request, auto: int = 0):
    """Acepta el archivo en cualquier campo del formulario (no exige el nombre `file` ni que
    FastAPI lo valide); si no llega ningún archivo, responde qué llegó en realidad."""
    try:
        form = await request.form()
    except Exception as e:
        log.warning("upload: formulario ilegible (%s) · %s", e, _req_summary(request))
        raise HTTPException(400, f"No se pudo leer el formulario: {e}")
    parts = [(k, type(v).__name__, getattr(v, "filename", None), getattr(v, "size", None) if hasattr(v, "read") else len(v))
             for k, v in form.multi_items()]
    log.info("upload %s · partes=%s", _req_summary(request), parts)
    file = next((v for _, v in form.multi_items() if isinstance(v, StarletteUploadFile)), None)
    if file is None:
        raise HTTPException(422, f"No llegó ningún archivo. Partes recibidas: {parts}")
    lote = request.query_params.get("lote")
    st = await run_in_threadpool(jobs.create, file.filename or "audio.m4a", file.file, "archivo", lote)
    if auto and st["estado"] == "cargado":
        st = jobs.enqueue(st["id"])
    base = str(request.base_url).rstrip("/")
    st["view_url"] = f"{base}/?job={st['id']}&k={config.RAIZ_TOKEN}"
    return st


@app.post("/api/atajo")
async def atajo(request: Request, auto: int = 0):
    """Destino del Atajo de iOS (hoja Compartir). Acepta formulario (cualquier campo con archivo)
    o el archivo crudo como cuerpo. Responde SOLO la URL a abrir, en texto plano, para que el
    Atajo la abra sin pasos intermedios. El audio queda cargado, listo para TRANSCRIBIR."""
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        f = next((v for v in form.values() if hasattr(v, "read")), None)
        if f is None:
            raise HTTPException(400, "No llegó ningún archivo")
        st = jobs.create(f.filename or _default_name(), f.file)
    else:
        with tempfile.SpooledTemporaryFile(max_size=32 << 20) as tmp:
            n = 0
            async for chunk in request.stream():
                tmp.write(chunk)
                n += len(chunk)
            if not n:
                raise HTTPException(400, "No llegó ningún archivo")
            tmp.seek(0)
            st = jobs.create(request.headers.get("x-filename") or _default_name(), tmp)
    if auto and st["estado"] == "cargado":
        st = jobs.enqueue(st["id"])
    base = str(request.base_url).rstrip("/")
    return PlainTextResponse(f"{base}/?job={st['id']}&k={config.RAIZ_TOKEN}")


def _default_name() -> str:
    return f"Grabación {datetime.now():%Y-%m-%d %H.%M}.m4a"


@app.post("/api/jobs/{jid}/transcribe")
def transcribe(jid: str):
    return jobs.enqueue(_exists(jid))


@app.get("/api/jobs")
def list_jobs(ids: str = ""):
    wanted = [i for i in ids.split(",") if i]
    for i in wanted:
        _exists(i)
    return jobs.list_jobs(ids=wanted or None)


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    st = jobs.read_state(_exists(jid))
    if st["estado"] == "listo":
        doc = jobs.load_doc(jid)
        st["doc"] = {
            "nota_diarizacion": doc["nota_diarizacion"], "diarizacion": doc["diarizacion"],
            "modelo": doc["modelo"], "proveedor": doc["proveedor"], "idioma": doc["idioma"],
            "speakers": [{**s, "mostrar": s["nombre"] or s["label"]} for s in doc["speakers"]],
            "items": [
                {"tipo": "gap", "ts": f"{transcript.ts(it['start'])}–{transcript.ts(it['end'])}"} if k == "gap" else
                {"tipo": "seg", "ts": transcript.ts(it["start"]),
                 "hablante": transcript.speaker_name(doc, it["speaker"]) if doc["diarizacion"] else "",
                 "texto": transcript.display_text(it)}
                for k, _, it in transcript.timeline(doc)],
        }
        st["txt"] = (jobs.job_dir(jid) / st["archivos"]["txt"]).read_text(encoding="utf-8")
    return st


@app.post("/api/jobs/{jid}/speakers")
async def rename_speakers(jid: str, request: Request):
    """Body: {"A": "Matías", "B": "Hernán"} — vacío vuelve a 'Hablante N'. Regenera TXT/MD/PDF."""
    names = await request.json()
    doc = jobs.load_doc(_exists(jid))
    for s in doc["speakers"]:
        if s["id"] in names:
            s["nombre"] = (names[s["id"]] or "").strip()[:60] or None
    jobs.save_doc(jid, doc)
    return {"ok": True}


@app.get("/api/jobs/{jid}/file/{kind}")
def download(jid: str, kind: str, inline: int = 0):
    st = jobs.read_state(_exists(jid))
    d = jobs.job_dir(jid)
    if kind == "original":
        storage.fetch(jid, f"original/{st['archivo_original']}")  # nube: lo trae del respaldo si hace falta
        p = d / "original" / st["archivo_original"]
        if not p.exists():
            raise HTTPException(404, "Audio original no disponible")
        return FileResponse(p, filename=p.name, content_disposition_type="inline" if inline else "attachment")
    elif kind in ("txt", "md", "pdf", "json") and st.get("archivos"):
        p = d / st["archivos"][kind]
    else:
        raise HTTPException(404)
    mt = {"txt": "text/plain; charset=utf-8", "md": "text/markdown; charset=utf-8",
          "pdf": "application/pdf", "json": "application/json"}.get(kind)
    if inline and kind == "md":  # Safari muestra text/plain; text/markdown lo descargaría
        mt = "text/plain; charset=utf-8"
    return FileResponse(p, media_type=mt, filename=p.name,
                        content_disposition_type="inline" if inline else "attachment")


def _exists(jid: str) -> str:
    try:
        if (jobs.job_dir(jid) / "estado.json").exists():
            return jid
    except ValueError:
        pass
    raise HTTPException(404, "No existe esa transcripción")


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()
