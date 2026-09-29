"""Exportación: TXT (limpio), MD (metadatos + timestamps, para ChatGPT/Claude) y PDF."""
import os
import unicodedata
from pathlib import Path

from fpdf import FPDF

from .transcript import display_text, speaker_name, timeline, ts


def _fecha(doc):
    return doc["fecha"].replace("T", " ")[:16]


def _dur(doc):
    return ts(doc["duracion"]) if doc.get("duracion") else "desconocida"


def to_txt(doc: dict) -> str:
    # Con timestamp y hablante por intervención (antes solo tenía el hablante): COPIAR y DESCARGAR TXT
    # comparten esta misma función — sin esto, el TXT se sentía "crudo" frente a la vista en pantalla,
    # que sí muestra hora por segmento (29-sep-2026, Cliente Cero).
    lines = []
    for kind, _, it in timeline(doc):
        if kind == "gap":
            lines.append(f"[{ts(it['start'])}–{ts(it['end'])}] (sin habla transcripta: silencio o inaudible)")
        else:
            who = speaker_name(doc, it["speaker"]) if doc["diarizacion"] else None
            prefix = f"[{ts(it['start'])}] {who}:" if who else f"[{ts(it['start'])}]"
            lines.append(f"{prefix} {display_text(it)}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def to_md(doc: dict) -> str:
    sp = ", ".join(speaker_name(doc, s["id"]) for s in doc["speakers"]) or "—"
    head = [
        f"# Transcripción — {doc['archivo_original']}", "",
        "| Campo | Valor |", "|---|---|",
        f"| Archivo | {doc['archivo_original']} |",
        f"| Fecha de transcripción | {_fecha(doc)} |",
        f"| Fecha del archivo | {doc.get('fecha_archivo', '—')} |",
        f"| Duración | {_dur(doc)} |",
        f"| Idioma | {doc['idioma']} |",
        f"| Proveedor / modelo | {doc['proveedor']} / {doc['modelo']} |",
        f"| Hablantes | {sp} |",
        f"| Diarización | {doc['nota_diarizacion']} |", "",
        "> Transcripción automática literal: no resumida ni corregida. "
        f"`palabra[?]` = baja confianza del reconocedor (< {doc['umbral_baja_confianza']}). "
        "Los tramos sin habla transcripta se indican con su intervalo.", "",
        "## Transcripción", "",
    ]
    body = []
    for kind, _, it in timeline(doc):
        if kind == "gap":
            body.append(f"*[{ts(it['start'])}–{ts(it['end'])}] sin habla transcripta (silencio o inaudible)*")
        else:
            who = speaker_name(doc, it["speaker"]) if doc["diarizacion"] else "Texto"
            body.append(f"**[{ts(it['start'])}] {who}:** {display_text(it)}")
        body.append("")
    return "\n".join(head + body).strip() + "\n"


class _PDF(FPDF):
    def footer(self):
        self.set_y(-12)
        self.set_font("U", "", 8)
        self.set_text_color(130, 130, 130)
        self.cell(0, 6, f"IMPULSARG-T TRANSCRIBE · página {self.page_no()}/{{nb}}", align="C")


def _fonts():
    """Arial en Windows (PC); DejaVu en Linux (nube, paquete fonts-dejavu-core). Ambas con Unicode completo."""
    win = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    if (win / "arial.ttf").exists():
        return win / "arial.ttf", win / "arialbd.ttf", win / "ariali.ttf"
    dv = Path("/usr/share/fonts/truetype/dejavu")
    reg = dv / "DejaVuSans.ttf"
    # si falta una variante (p. ej. Oblique no viene en fonts-dejavu-core) se usa la regular: el PDF nunca falla por estilo
    pick = lambda f: f if f.exists() else reg
    return reg, pick(dv / "DejaVuSans-Bold.ttf"), pick(dv / "DejaVuSans-Oblique.ttf")


def to_pdf(doc: dict, path: Path):
    regular, bold, italic = _fonts()
    pdf = _PDF(format="A4")
    pdf.add_font("U", "", str(regular))
    pdf.add_font("U", "B", str(bold))
    pdf.add_font("U", "I", str(italic))
    pdf.set_auto_page_break(True, margin=18)
    pdf.set_title(f"Transcripción - {doc['archivo_original']}")
    pdf.add_page()
    pdf.set_font("U", "B", 16)
    pdf.cell(0, 9, "IMPULSARG-T TRANSCRIBE", new_x="LMARGIN", new_y="NEXT", align="L")
    pdf.set_font("U", "", 10)
    pdf.set_text_color(60, 60, 60)
    for k, v in [("Archivo", doc["archivo_original"]), ("Fecha", _fecha(doc)),
                 ("Fecha del archivo", doc.get("fecha_archivo", "—")), ("Duración", _dur(doc)),
                 ("Idioma", doc["idioma"]), ("Proveedor / modelo", f"{doc['proveedor']} / {doc['modelo']}"),
                 ("Hablantes", ", ".join(speaker_name(doc, s["id"]) for s in doc["speakers"]) or "—"),
                 ("Diarización", doc["nota_diarizacion"])]:
        pdf.multi_cell(0, 5, f"{k}: {v}", new_x="LMARGIN", new_y="NEXT", align="L")
    pdf.set_font("U", "I", 8)
    pdf.multi_cell(0, 4.5, "Transcripción automática literal: no resumida ni corregida. "
                   f"palabra[?] = baja confianza del reconocedor.", new_x="LMARGIN", new_y="NEXT", align="L")
    pdf.ln(3)
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("U", "B", 13)
    pdf.cell(0, 8, "TRANSCRIPCIÓN", new_x="LMARGIN", new_y="NEXT", align="L")
    pdf.ln(1)
    for kind, _, it in timeline(doc):
        if kind == "gap":
            pdf.set_font("U", "I", 9)
            pdf.set_text_color(120, 120, 120)
            pdf.multi_cell(0, 5, f"[{ts(it['start'])}–{ts(it['end'])}] sin habla transcripta (silencio o inaudible)",
                           new_x="LMARGIN", new_y="NEXT", align="L")
            pdf.ln(1.5)
            continue
        who = speaker_name(doc, it["speaker"]) if doc["diarizacion"] else "Texto"
        if pdf.get_y() > pdf.h - pdf.b_margin - 18:  # que el encabezado no quede solo al pie
            pdf.add_page()
        pdf.set_font("U", "B", 10)
        pdf.set_text_color(40, 40, 40)
        pdf.multi_cell(0, 5.5, f"[{ts(it['start'])}] {who}", new_x="LMARGIN", new_y="NEXT", align="L")
        pdf.set_font("U", "", 11)
        pdf.set_text_color(0, 0, 0)
        pdf.multi_cell(0, 6, display_text(it), new_x="LMARGIN", new_y="NEXT", align="L")
        pdf.ln(2)
    tmp = path.with_suffix(".tmp")
    pdf.output(str(tmp))
    os.replace(tmp, path)


def write_all(doc: dict, folder: Path, stem: str) -> dict:
    # iOS nombra archivos en Unicode NFD (ó = o + acento suelto): se normaliza a NFC para mostrar bien
    doc = {**doc, "archivo_original": unicodedata.normalize("NFC", doc["archivo_original"])}
    files = {"txt": folder / f"{stem}.txt", "md": folder / f"{stem}.md", "pdf": folder / f"{stem}.pdf"}
    files["txt"].write_text(to_txt(doc), encoding="utf-8")
    files["md"].write_text(to_md(doc), encoding="utf-8")
    to_pdf(doc, files["pdf"])
    return {k: v.name for k, v in files.items()}
