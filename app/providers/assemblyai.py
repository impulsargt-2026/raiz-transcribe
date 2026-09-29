"""AssemblyAI async (pre-recorded). Docs verificadas 2026-09-28:
- POST /v2/upload (máx 2,2 GB) → upload_url; POST /v2/transcript (máx 10 h de audio); GET para polling.
- speech_models: universal-3-5-pro (español incluido) con fallback universal-2.
- speaker_labels=true → utterances con speaker A/B/C, start/end en ms.
Sin prompt ni "mejoras" de LLM: se pide transcripción literal, nada de resumen.
"""
import time
from pathlib import Path

import httpx

from .. import config
from . import ProviderResult

BASE = "https://api.assemblyai.com"
MODELS = ["universal-3-5-pro", "universal-2"]


class AssemblyAIProvider:
    name = "AssemblyAI"
    model = "+".join(MODELS)

    def _headers(self):
        key = config.api_key("ASSEMBLYAI_API_KEY")
        if not key:
            raise RuntimeError("Falta ASSEMBLYAI_API_KEY en el archivo .env de RAÍZ Transcribe.")
        return {"authorization": key}

    def transcribe(self, audio: Path | None, language: str, on_status=lambda s: None,
                   on_submitted=lambda tid: None, transcript_id: str | None = None):
        """Si `transcript_id` viene de un intento anterior (reinicio del servidor), solo se consulta:
        no se vuelve a subir ni a cobrar. `on_submitted` permite guardar el id apenas existe."""
        h = self._headers()
        timeout = httpx.Timeout(60, read=600, write=600)
        with httpx.Client(timeout=timeout) as c:
            tid = transcript_id
            if not tid:
                on_status("Subiendo audio al proveedor")

                def chunks():
                    with audio.open("rb") as f:
                        while b := f.read(1 << 20):
                            yield b

                r = c.post(f"{BASE}/v2/upload", headers=h, content=chunks())
                _check(r)
                upload_url = r.json()["upload_url"]

                body = {"audio_url": upload_url, "speech_models": MODELS, "language_code": language,
                        "speaker_labels": True, "punctuate": True, "format_text": True}
                r = c.post(f"{BASE}/v2/transcript", headers=h, json=body)
                _check(r)
                tid = r.json()["id"]
                on_submitted(tid)
            on_status("Transcribiendo e identificando hablantes")
            while True:
                time.sleep(4)
                r = c.get(f"{BASE}/v2/transcript/{tid}", headers=h)
                _check(r)
                raw = r.json()
                if raw["status"] == "completed":
                    break
                if raw["status"] == "error":
                    raise RuntimeError(f"AssemblyAI devolvió error: {raw.get('error')}")

        utts = []
        for u in raw.get("utterances") or []:
            utts.append({
                "start": u["start"] / 1000, "end": u["end"] / 1000, "speaker_id": u.get("speaker"),
                "text": u["text"], "confidence": u.get("confidence"),
                "words": [{"text": w["text"], "start": w["start"] / 1000, "end": w["end"] / 1000,
                           "confidence": w.get("confidence"), "speaker_id": w.get("speaker")}
                          for w in u.get("words") or []],
            })
        if not utts and raw.get("text"):  # sin diarización disponible: un bloque sin hablante
            ws = raw.get("words") or []
            utts.append({"start": (ws[0]["start"] / 1000) if ws else 0,
                         "end": (ws[-1]["end"] / 1000) if ws else 0, "speaker_id": None,
                         "text": raw["text"], "confidence": raw.get("confidence"),
                         "words": [{"text": w["text"], "start": w["start"] / 1000, "end": w["end"] / 1000,
                                    "confidence": w.get("confidence"), "speaker_id": None} for w in ws]})
        return raw, ProviderResult(
            model=raw.get("speech_model_used") or self.model,
            language=raw.get("language_code") or language,
            duration=raw.get("audio_duration"),
            diarization=bool(raw.get("utterances")),
            utterances=utts,
        )


def _check(r: httpx.Response):
    if r.status_code >= 400:
        # nunca incluir headers (llevan la API key)
        raise RuntimeError(f"AssemblyAI HTTP {r.status_code}: {r.text[:300]}")
