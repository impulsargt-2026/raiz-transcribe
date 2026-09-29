"""Configuración: lee .env (fuera del repo) y define rutas. Nunca loguear secretos."""
import os
import secrets
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
ENV_FILE = ROOT_DIR / ".env"


def _load_env():
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def _ensure_token():
    """Clave de acceso para usar la app desde otros dispositivos de la red (no es la API key)."""
    if os.environ.get("RAIZ_TOKEN"):
        return
    if os.environ.get("RAIZ_CLOUD") == "1":  # en la nube la clave es un secreto fijo (si cambiara, el ícono dejaría de entrar)
        raise RuntimeError("Falta la variable secreta RAIZ_TOKEN en el servicio en la nube.")
    tok = secrets.token_urlsafe(9)
    with ENV_FILE.open("a", encoding="utf-8") as f:
        f.write(f"\nRAIZ_TOKEN={tok}\n")
    os.environ["RAIZ_TOKEN"] = tok


_load_env()
_ensure_token()



def api_key(name: str) -> str:
    """Relee .env en cada uso: al pegar la key no hace falta reiniciar el servidor."""
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(name + "="):
                return line.split("=", 1)[1].strip()
    return os.environ.get(name, "")


ASSEMBLYAI_API_KEY = api_key("ASSEMBLYAI_API_KEY")
RAIZ_TOKEN = os.environ["RAIZ_TOKEN"]
PORT = int(os.environ.get("RAIZ_PORT", "8019"))
LANGUAGE = os.environ.get("RAIZ_LANGUAGE", "es")
# Nube: sin excepción "misma PC", sin normalizar (AssemblyAI acepta m4a/mp4/webm/mov; ahorra CPU)
CLOUD = os.environ.get("RAIZ_CLOUD", "0") == "1"
NORMALIZE = os.environ.get("RAIZ_NORMALIZE", "0" if CLOUD else "1") == "1"
# Datos (audios y transcripciones) FUERA del repositorio: pueden contener reuniones privadas.
DATA_DIR = Path(os.environ.get("RAIZ_DATA_DIR", str(Path.home() / "RAIZ_Transcribe_datos")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
