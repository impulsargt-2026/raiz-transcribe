"""Normalización + diarización → fuente estructurada (transcripcion.json).

Regla: TRANSCRIBIR ≠ REESCRIBIR. Acá no se corrige, completa ni resume texto.
Solo se ordena lo que devolvió el proveedor, se etiquetan hablantes y se marcan
(sin inventar) las palabras de baja confianza y los tramos sin habla transcripta.
"""
import os

LOW_CONF = float(os.environ.get("RAIZ_LOW_CONF", "0.35"))
GAP_SECONDS = float(os.environ.get("RAIZ_GAP_SECONDS", "6"))


def ts(sec: float) -> str:
    sec = int(sec or 0)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def build(meta: dict, result) -> dict:
    order = []
    for u in result.utterances:
        if u["speaker_id"] is not None and u["speaker_id"] not in order:
            order.append(u["speaker_id"])
    speakers = [{"id": sid, "label": f"Hablante {i + 1}", "nombre": None} for i, sid in enumerate(order)]

    segments = [{"start": round(u["start"], 2), "end": round(u["end"], 2), "speaker": u["speaker_id"],
                 "text": u["text"], "confidence": u.get("confidence"), "words": u.get("words", [])}
                for u in sorted(result.utterances, key=lambda x: x["start"])]

    gaps, prev_end = [], 0.0
    for s in segments:
        if s["start"] - prev_end >= GAP_SECONDS:
            gaps.append({"start": round(prev_end, 2), "end": s["start"]})
        prev_end = max(prev_end, s["end"])

    if not result.diarization:
        nota = "El proveedor no devolvió separación de hablantes: el texto se muestra sin atribuir."
    elif len(speakers) == 1:
        nota = "El proveedor detectó 1 solo hablante."
    else:
        nota = f"Hablantes separados automáticamente por el proveedor ({len(speakers)} detectados). Etiquetas no verificadas por humano."

    return {**meta, "idioma": result.language, "modelo": result.model,
            "duracion": result.duration or meta.get("duracion"),
            "diarizacion": result.diarization, "nota_diarizacion": nota,
            "umbral_baja_confianza": LOW_CONF, "speakers": speakers,
            "segments": segments, "tramos_sin_habla": gaps}


def speaker_name(doc: dict, sid) -> str:
    for s in doc["speakers"]:
        if s["id"] == sid:
            return s["nombre"] or s["label"]
    return "Sin hablante"


def display_text(seg: dict, mark_low=True) -> str:
    """Texto del segmento; las palabras de baja confianza se marcan con [?] (no se reemplazan)."""
    if not mark_low or not seg.get("words"):
        return seg["text"]
    out = []
    for w in seg["words"]:
        c = w.get("confidence")
        out.append(f"{w['text']}[?]" if c is not None and c < LOW_CONF else w["text"])
    return " ".join(out)


def timeline(doc: dict):
    """Segmentos + marcas de tramos sin habla, en orden temporal."""
    items = [("seg", s["start"], s) for s in doc["segments"]]
    items += [("gap", g["start"], g) for g in doc.get("tramos_sin_habla", [])]
    return sorted(items, key=lambda x: (x[1], x[0] == "seg"))
