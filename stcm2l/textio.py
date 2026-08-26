"""
stcm2l.textio
=============

Deteccao de codificacao, protecao de marcadores da engine e leitura/escrita dos
formatos de trabalho do tradutor (.json e .txt).

Marcadores preservados (nunca traduzidos):
    #Name[2]  #KW_F[]  #KW_ED[]  #ANY[...]   -> tags Otomate/Rejet
    {..}  <..>  %VAR%  $VAR                  -> variaveis de engine
    \\n \\c \\r \\t                            -> controles embutidos no texto
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable

from .core import DataBlock, Script

# ---------------------------------------------------------------------------
# Codificacoes
# ---------------------------------------------------------------------------

#: ordem de tentativa. cp932 == Shift-JIS estendido da Microsoft (o usado no Vita).
#: utf-16-le fica por ULTIMO de proposito: ele decodifica quase qualquer coisa de
#: tamanho par, entao so vence se decodificar ESTRITAMENTE mais blocos que os outros.
CANDIDATE_ENCODINGS = ("utf-8", "cp932", "cp1252", "latin-1", "utf-16-le")

#: caracteres de controle aceitos dentro de um texto de jogo
_ALLOWED_CTRL = {0x09, 0x0A, 0x0D}


def decode_block(raw: bytes, encoding: str) -> str | None:
    """Decodifica os bytes uteis; None se a codificacao nao servir."""
    try:
        return raw.decode(encoding)
    except UnicodeDecodeError:
        return None


def looks_like_text(text: str) -> bool:
    """Heuristica: o conteudo decodificado parece dialogo/legenda e nao binario?"""
    if not text:
        return False
    bad = 0
    for ch in text:
        cp = ord(ch)
        if cp in _ALLOWED_CTRL:
            continue
        if cp < 0x20 or cp == 0x7F:
            bad += 1
            continue
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cs", "Co", "Cn"):
            bad += 1
    return bad == 0


def detect_encoding(script: Script, forced: str | None = None) -> str:
    """Escolhe a codificacao que decodifica o maior numero de blocos do arquivo."""
    if forced:
        return forced
    blobs = [db.content for _, _, db in script.iter_data_blocks() if db.content]
    if not blobs:
        return "utf-8"
    best, best_score = "utf-8", -1
    for enc in CANDIDATE_ENCODINGS:
        score = 0
        for raw in blobs:
            txt = decode_block(raw, enc)
            if txt is not None and looks_like_text(txt):
                score += 1
        if score > best_score:
            best, best_score = enc, score
        if score == len(blobs):
            break
    return best


def encode_text(text: str, encoding: str, fallback: str = "strict") -> bytes:
    """
    Codifica o texto traduzido.

    fallback="strict"   : erro se algum caractere nao couber (recomendado, avisa cedo)
    fallback="ascii"    : remove acentos (ç->c, á->a) antes de codificar
    fallback="replace"  : substitui o impossivel por '?'
    """
    try:
        return text.encode(encoding)
    except UnicodeEncodeError:
        if fallback == "ascii":
            folded = unicodedata.normalize("NFKD", text)
            folded = "".join(c for c in folded if not unicodedata.combining(c))
            return folded.encode(encoding, errors="replace")
        if fallback == "replace":
            return text.encode(encoding, errors="replace")
        raise


# ---------------------------------------------------------------------------
# Protecao de marcadores
# ---------------------------------------------------------------------------

TAG_PATTERN = re.compile(
    r"""(
        \#\w+\[[^\]]*\]      # #Name[2] , #KW_F[] , #KW_ED[]
      | \#\w+                # #Tag solta
      | \{[^{}]*\}           # {var}
      | <[^<>]{0,64}>        # <tag>
      | %[A-Za-z0-9_]+%      # %VAR%
      | \$[A-Za-z0-9_]+      # $VAR
      | \\[a-zA-Z]           # \n \c \r \t
    )""",
    re.VERBOSE,
)

#: delimitadores raros o bastante para sobreviver a um tradutor automatico
PH_OPEN, PH_CLOSE = "\u27e6", "\u27e7"     # ⟦0⟧
_PH_RE = re.compile(re.escape(PH_OPEN) + r"(\d+)" + re.escape(PH_CLOSE))


def protect_tags(text: str) -> tuple[str, list[str]]:
    """Troca marcadores por placeholders numerados antes de traduzir."""
    tags: list[str] = []

    def _sub(m: re.Match) -> str:
        tags.append(m.group(0))
        return f"{PH_OPEN}{len(tags) - 1}{PH_CLOSE}"

    return TAG_PATTERN.sub(_sub, text), tags


def restore_tags(text: str, tags: list[str]) -> tuple[str, bool]:
    """
    Recoloca os marcadores. Retorna (texto, ok) - ok=False quando o tradutor
    destruiu/perdeu algum placeholder (entrada fica marcada para revisao).
    """
    seen: set[int] = set()

    def _sub(m: re.Match) -> str:
        idx = int(m.group(1))
        if idx >= len(tags):
            return m.group(0)
        seen.add(idx)
        return tags[idx]

    out = _PH_RE.sub(_sub, text)
    ok = len(seen) == len(tags) and PH_OPEN not in out and PH_CLOSE not in out
    if not ok:
        # nao perde os marcadores: reanexa os que sumiram no fim da linha
        missing = [t for i, t in enumerate(tags) if i not in seen]
        if missing:
            out = out + "".join(missing)
    return out, ok


# ---------------------------------------------------------------------------
# Entrada de texto
# ---------------------------------------------------------------------------

@dataclass
class TextEntry:
    id: str
    elem: int
    seg: int
    opcode: int | None
    encoding: str
    original: str
    translation: str = ""
    max_bytes: int = 0            # tamanho original em bytes (referencia)
    needs_review: bool = False
    notes: list[str] = field(default_factory=list)

    @staticmethod
    def make_id(elem: int, seg: int) -> str:
        return f"A{elem:05d}_S{seg:02d}"

    @property
    def final(self) -> str:
        return self.translation if self.translation.strip() else self.original


# ---------------------------------------------------------------------------
# Escape para o formato TXT (uma linha por texto)
# ---------------------------------------------------------------------------

def esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")


def unesc(text: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt == "n":
                out.append("\n"); i += 2; continue
            if nxt == "r":
                out.append("\r"); i += 2; continue
            if nxt == "\\":
                out.append("\\"); i += 2; continue
        out.append(ch)
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Serializacao do arquivo de trabalho
# ---------------------------------------------------------------------------

def dump_entries(entries: list[TextEntry], path: Path, source: str,
                 encoding: str, fmt: str = "json") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        payload = {
            "tool": "stcm2l-tool",
            "version": 1,
            "source": source,
            "encoding": encoding,
            "count": len(entries),
            "entries": [asdict(e) for e in entries],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return

    lines = [
        f"# stcm2l-tool | fonte: {source} | encoding: {encoding}",
        "# Traduza SOMENTE as linhas iniciadas por '>'. Nao altere os [IDs].",
        "# '\\n' = quebra de linha real; '\\\\' = barra invertida literal.",
        "",
    ]
    for e in entries:
        lines.append(f"[{e.id}]")
        lines.append(f"< {esc(e.original)}")
        lines.append(f"> {esc(e.translation)}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def load_entries(path: Path) -> tuple[list[TextEntry], dict[str, Any]]:
    """Le .json ou .txt produzido por dump_entries."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json" or text.lstrip().startswith("{"):
        payload = json.loads(text)
        entries = []
        for raw in payload.get("entries", []):
            entries.append(TextEntry(
                id=raw["id"],
                elem=raw.get("elem", -1),
                seg=raw.get("seg", -1),
                opcode=raw.get("opcode"),
                encoding=raw.get("encoding", payload.get("encoding", "utf-8")),
                original=raw.get("original", ""),
                translation=raw.get("translation", ""),
                max_bytes=raw.get("max_bytes", 0),
                needs_review=raw.get("needs_review", False),
                notes=raw.get("notes", []),
            ))
        return entries, payload

    entries = []
    meta: dict[str, Any] = {}
    cur_id: str | None = None
    original = translation = ""
    for line in text.splitlines():
        if line.startswith("#"):
            if "encoding:" in line:
                meta["encoding"] = line.split("encoding:")[1].strip()
            continue
        if line.startswith("[") and line.endswith("]"):
            if cur_id:
                entries.append(_txt_entry(cur_id, original, translation, meta))
            cur_id, original, translation = line[1:-1].strip(), "", ""
        elif line.startswith("< "):
            original = unesc(line[2:])
        elif line == "<":
            original = ""
        elif line.startswith("> "):
            translation = unesc(line[2:])
        elif line == ">":
            translation = ""
    if cur_id:
        entries.append(_txt_entry(cur_id, original, translation, meta))
    return entries, meta


def _txt_entry(entry_id: str, original: str, translation: str,
               meta: dict[str, Any]) -> TextEntry:
    elem, seg = -1, -1
    m = re.match(r"A(\d+)_S(\d+)$", entry_id)
    if m:
        elem, seg = int(m.group(1)), int(m.group(2))
    return TextEntry(
        id=entry_id, elem=elem, seg=seg, opcode=None,
        encoding=meta.get("encoding", "utf-8"),
        original=original, translation=translation,
    )
