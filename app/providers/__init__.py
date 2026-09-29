"""Proveedores de transcripción intercambiables.

Contrato: un proveedor expone `name`, `model` y `transcribe(audio_path, language, on_status)`,
y devuelve (crudo: dict, resultado: ProviderResult). Para cambiar de proveedor se agrega
un módulo nuevo acá y se cambia RAIZ_PROVIDER; interfaz, normalización y exportación no cambian.
"""
import os
from dataclasses import dataclass, field


@dataclass
class ProviderResult:
    model: str
    language: str
    duration: float | None
    diarization: bool
    # cada utterance: {start, end (segundos), speaker_id (str|None), text, confidence, words:[{text,start,end,confidence,speaker_id}]}
    utterances: list = field(default_factory=list)


def get_provider():
    name = os.environ.get("RAIZ_PROVIDER", "assemblyai")
    if name == "assemblyai":
        from .assemblyai import AssemblyAIProvider
        return AssemblyAIProvider()
    raise ValueError(f"Proveedor desconocido: {name}")
