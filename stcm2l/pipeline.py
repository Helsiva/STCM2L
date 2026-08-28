"""
stcm2l.pipeline
===============

Operacoes de alto nivel: inspecao, verificacao (round-trip), extracao e
injecao - individuais ou em lote.
"""

from __future__ import annotations

import struct
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .core import (
    ACTION_HEADER_SIZE, PARAM_SIZE, TAG_CODE_END, TAG_CODE_START, TAG_GLOBAL_DATA,
    DataBlock, RawSegment, Script, Stcm2lError, build, parse, roundtrip_check,
)

#: um trecho cru que comeca com um marcador conhecido e area de dados, nao codigo
KNOWN_TAGS = (TAG_CODE_START, TAG_CODE_END, TAG_GLOBAL_DATA)
from .textio import (
    MAX_LINE_DEFAULT, TextEntry, classify_text, decode_block, detect_encoding,
    detect_newline, dump_entries, encode_text, load_entries, looks_like_text,
    wrap_text,
)

DAT_SUFFIXES = (".dat", ".bin", ".stcm", ".scb")


# ---------------------------------------------------------------------------
# Utilidades de arquivo
# ---------------------------------------------------------------------------

def iter_inputs(target: Path, recursive: bool = False,
                suffixes: Iterable[str] = DAT_SUFFIXES) -> list[Path]:
    """Resolve um caminho (arquivo ou pasta) na lista de arquivos a processar."""
    if target.is_file():
        return [target]
    if not target.is_dir():
        raise Stcm2lError(f"caminho inexistente: {target}")
    pattern = "**/*" if recursive else "*"
    sufs = {s.lower() for s in suffixes}
    return sorted(p for p in target.glob(pattern)
                  if p.is_file() and p.suffix.lower() in sufs)


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------

@dataclass
class Info:
    path: Path
    size: int
    magic: str
    export_offset: int
    export_count: int
    collection_link: int
    actions: int
    raw_chunks: int
    data_blocks: int
    text_blocks: int
    encoding: str
    opcodes: list[tuple[int, int]]
    warnings: list[str]
    #: quantos textos de cada tipo, e onde moram ("acao" = embutido numa acao,
    #: "cru" = pool de strings solto, tipicamente na cauda do arquivo)
    kinds: dict[str, int] = None  # type: ignore[assignment]
    where: dict[str, dict[str, int]] = None  # type: ignore[assignment]


def inspect(path: Path, forced_encoding: str | None = None) -> Info:
    data = path.read_bytes()
    script = parse(data)
    encoding = detect_encoding(script, forced_encoding)
    actions = sum(1 for e in script.elements if e.kind == "action")
    blocks = list(script.iter_data_blocks())
    texts = 0
    kinds = {"cjk": 0, "prose": 0, "id": 0}
    where = {"acao": dict(kinds), "cru": dict(kinds)}
    for ei, _, db in blocks:
        txt = decode_block(db.content, encoding)
        if txt and looks_like_text(txt):
            texts += 1
            kind = classify_text(txt)
            kinds[kind] += 1
            where["acao" if script.elements[ei].kind == "action" else "cru"][kind] += 1
    counter: Counter[int] = Counter(e.opcode for e in script.elements if e.kind == "action")
    return Info(
        path=path, size=len(data), magic=script.header.magic_text,
        export_offset=script.header.export_offset,
        export_count=script.header.export_count,
        collection_link=script.header.collection_link,
        actions=actions,
        raw_chunks=sum(1 for e in script.elements if e.kind == "raw"),
        data_blocks=len(blocks), text_blocks=texts, encoding=encoding,
        opcodes=counter.most_common(12), warnings=list(script.warnings),
        kinds=kinds, where=where,
    )


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------

def collect_entries(script: Script, encoding: str, all_blocks: bool = False,
                    min_chars: int = 1) -> list[TextEntry]:
    """Transforma os blocos de dado do script em entradas de traducao."""
    entries: list[TextEntry] = []
    for elem, seg, db in script.iter_data_blocks():
        raw = db.content
        if not raw:
            continue
        text = decode_block(raw, encoding)
        if text is None:
            if not all_blocks:
                continue
            text = raw.decode(encoding, errors="replace")
        if not all_blocks:
            if not looks_like_text(text) or len(text.strip()) < min_chars:
                continue
        el = script.elements[elem]
        entries.append(TextEntry(
            id=TextEntry.make_id(elem, seg),
            elem=elem, seg=seg,
            opcode=el.opcode if el.kind == "action" else None,
            encoding=encoding, original=text, translation="",
            max_bytes=len(raw),
        ))
    return entries


def extract_file(path: Path, out_path: Path, fmt: str = "json",
                 forced_encoding: str | None = None, all_blocks: bool = False) -> int:
    data = path.read_bytes()
    script = parse(data)
    encoding = detect_encoding(script, forced_encoding)
    entries = collect_entries(script, encoding, all_blocks=all_blocks)
    dump_entries(entries, out_path, source=path.name, encoding=encoding, fmt=fmt)
    return len(entries)


# ---------------------------------------------------------------------------
# inject
# ---------------------------------------------------------------------------

@dataclass
class InjectReport:
    source: Path
    output: Path
    applied: int = 0
    skipped: int = 0
    grown: int = 0
    length_params_fixed: int = 0
    problems: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.problems is None:
            self.problems = []


def _fix_length_params(script: Script, elem: int, old_lens: set[int], new_len: int) -> int:
    """
    Alguns opcodes guardam o tamanho da string tambem como imediato num parametro.
    Quando o valor bate exatamente com o tamanho antigo, atualizamos junto.
    """
    el = script.elements[elem]
    if el.kind != "action":
        return 0
    fixed = 0
    for p in el.params:
        for wi in range(3):
            if p[wi] in old_lens and p[wi] not in (0, 1):
                p[wi] = new_len
                fixed += 1
    return fixed


def inject_file(dat_path: Path, texts_path: Path, out_path: Path,
                out_encoding: str | None = None, fallback: str = "strict",
                relocate: str = "scan", fix_len_params: bool = True,
                strict_match: bool = False,
                max_line: int = MAX_LINE_DEFAULT,
                newline: str = "auto") -> InjectReport:
    """
    Injeta o texto traduzido de volta no .DAT, recalculando todos os ponteiros.

    `max_line` quebra a fala traduzida em linhas de ate N colunas visiveis antes
    de codificar, para caber na caixa de texto do jogo (0 desliga). E uma rede de
    seguranca para texto editado a mao depois do `translate`: como wrap_text() e
    idempotente, o que ja veio quebrado nao muda.
    """
    data = dat_path.read_bytes()
    script = parse(data)
    entries, meta = load_entries(texts_path)
    encoding = out_encoding or meta.get("encoding") or detect_encoding(script)
    nl = detect_newline(entries, None if newline == "auto" else newline)
    report = InjectReport(source=dat_path, output=out_path)

    for entry in entries:
        translation = entry.final
        if not translation:
            report.skipped += 1
            continue
        if not (0 <= entry.elem < len(script.elements)):
            report.problems.append(f"{entry.id}: elemento {entry.elem} fora do arquivo")
            report.skipped += 1
            continue
        el = script.elements[entry.elem]
        if not (0 <= entry.seg < len(el.segments)) or not isinstance(el.segments[entry.seg], DataBlock):
            report.problems.append(f"{entry.id}: segmento {entry.seg} nao e um bloco de dado")
            report.skipped += 1
            continue

        db: DataBlock = el.segments[entry.seg]
        current = decode_block(db.content, encoding)
        if entry.original and current is not None and current != entry.original:
            msg = (f"{entry.id}: texto original divergente "
                   f"(.DAT != arquivo de traducao) - o .DAT mudou desde a extracao?")
            if strict_match:
                raise Stcm2lError(msg)
            report.problems.append(msg)

        if translation == entry.original:
            report.skipped += 1
            continue

        # so traducao passa pelo wrap - o texto original nunca e reescrito
        if max_line > 0 and entry.translation.strip():
            translation = wrap_text(translation, max_line, nl)

        blob = encode_text(translation, encoding, fallback=fallback)
        old_lens = {db.raw_len, db.padded_len, len(db.content)}
        old_size = db.size()
        db.set_content(blob)
        if db.raw_len > max(old_lens):
            report.grown += 1
        risky_raw = (el.kind == "raw" and el.segments
                     and isinstance(el.segments[0], RawSegment)
                     and not el.segments[0].data.startswith(KNOWN_TAGS))
        if risky_raw and db.size() != old_size:
            report.problems.append(
                f"{entry.id}: bloco redimensionado dentro de um trecho NAO reconhecido como "
                f"acao (offset 0x{el.offset:X}). Se houver um cabecalho de acao ali, o campo "
                f"'length' dele nao sera atualizado. Rode 'verify' neste arquivo antes de usar "
                f"no jogo, ou mantenha o texto do mesmo tamanho."
            )
        if fix_len_params:
            report.length_params_fixed += _fix_length_params(
                script, entry.elem, old_lens - {0, 1}, len(db.content)
            )
        report.applied += 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(build(script, relocate=relocate))

    # sanidade: o arquivo gerado tem que reabrir sem erros
    try:
        check = parse(out_path.read_bytes())
        if len(check.elements) != len(script.elements):
            report.problems.append(
                "aviso: o arquivo gerado reabriu com contagem de elementos diferente "
                f"({len(check.elements)} vs {len(script.elements)})"
            )
    except Stcm2lError as exc:
        report.problems.append(f"ERRO: o arquivo gerado nao reabre: {exc}")
    return report


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

def verify_file(path: Path, relocate: str = "scan") -> tuple[bool, str]:
    return roundtrip_check(path.read_bytes(), relocate=relocate)
