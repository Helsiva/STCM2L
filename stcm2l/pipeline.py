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
    encoding_report,
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
    #: (codificacao, blocos que ela le) para cada candidata, e o total de blocos
    encoding_scores: list[tuple[str, int]] = None  # type: ignore[assignment]
    encoding_total: int = 0
    #: a mesma frase lida por cada candidata - mojibake fica obvio aqui
    encoding_samples: list[dict[str, str]] = None  # type: ignore[assignment]


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
    placar, total_blocos, exemplos = encoding_report(script)
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
        encoding_scores=placar, encoding_total=total_blocos, encoding_samples=exemplos,
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
    #: por que cada entrada foi pulada - "0 aplicados" sem isso nao diz nada
    skip_sem_traducao: int = 0
    skip_igual: int = 0
    skip_alvo: int = 0
    skip_divergente: int = 0
    #: entradas cujo texto original nao bate com o bloco do .DAT
    divergentes: int = 0
    #: entradas recusadas por nao caber no bloco original (modo --fit)
    overflow: int = 0
    #: maior estouro visto, em bytes (para dimensionar o corte manual)
    worst_overflow: int = 0
    #: identificadores que a traducao alterou (o jogo procura o recurso por nome)
    id_changes: int = 0
    skip_id: int = 0
    #: True quando a saida tem exatamente o mesmo layout do original
    layout_preserved: bool = False
    #: codificacao efetivamente usada para LER o .DAT
    src_encoding: str = ""
    #: (acertos, testados) do casamento entre o .json e o .DAT
    match_originais: tuple[int, int] = (0, 0)
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


def _resolve_src_encoding(script: Script, entries: list[TextEntry],
                          declared: str | None) -> tuple[str, int, int, str | None]:
    """
    Descobre em que codificacao LER o .DAT, medindo em vez de acreditar.

    O criterio e direto: qual codificacao faz o texto do bloco bater com o
    campo `original` do arquivo de traducao. A declarada no meta do .json pode
    estar errada (o extract detectou torto, ou o .json veio de outro lote), e
    acreditar nela transforma o arquivo inteiro em "texto original divergente"
    - centenas de avisos apontando para o arquivo errado.

    Devolve (codificacao, acertos, testados, aviso).
    """
    amostra = [e for e in entries if e.original][:300]
    if not amostra:
        return declared or detect_encoding(script), 0, 0, None

    candidatas: list[str] = []
    for enc in (declared, detect_encoding(script), "cp932", "utf-8", "utf-16-le"):
        if enc and enc not in candidatas:
            candidatas.append(enc)

    def acertos(enc: str) -> int:
        n = 0
        for entry in amostra:
            if not (0 <= entry.elem < len(script.elements)):
                continue
            el = script.elements[entry.elem]
            if not (0 <= entry.seg < len(el.segments)):
                continue
            seg = el.segments[entry.seg]
            if not isinstance(seg, DataBlock):
                continue
            if decode_block(seg.content, enc) == entry.original:
                n += 1
        return n

    marcadas = [(acertos(enc), -i, enc) for i, enc in enumerate(candidatas)]
    melhor, _, escolhida = max(marcadas)
    aviso = None
    if declared and escolhida != declared:
        base = dict((enc, n) for n, _, enc in marcadas)
        aviso = (f"o arquivo de traducao diz que o .DAT esta em '{declared}' "
                 f"({base.get(declared, 0)} de {len(amostra)} textos batem), mas em "
                 f"'{escolhida}' batem {melhor}. Lendo como '{escolhida}'.")
    return escolhida, melhor, len(amostra), aviso


def inject_file(dat_path: Path, texts_path: Path, out_path: Path,
                out_encoding: str | None = None, fallback: str = "strict",
                relocate: str = "scan", fix_len_params: bool = False,
                strict_match: bool = False,
                ignore_mismatch: bool = False,
                max_line: int = MAX_LINE_DEFAULT,
                newline: str = "auto",
                fit: bool = False,
                allow_id_change: bool = False) -> InjectReport:
    """
    Injeta o texto traduzido de volta no .DAT.

    Dois modos, e a escolha muda o risco:

    `fit=False` (padrao) - o bloco cresce e o arquivo inteiro e reendereçado.
        Cabe qualquer traducao, mas depende de acertar QUAIS words do script sao
        ponteiro. Um imediato do jogo (numero de flag, alvo de salto) que por
        acaso valha um offset conhecido e reescrito junto e o roteiro desanda.

    `fit=True` - nenhum bloco muda de tamanho: o que sobra vira padding e o que
        nao cabe e recusado (fica o japones). O arquivo de saida tem o MESMO
        tamanho e o MESMO layout do original, entao nao existe ponteiro para
        errar. E o modo a usar quando o jogo comeca a se perder depois do patch.

    `max_line` quebra a fala traduzida em linhas de ate N colunas visiveis antes
    de codificar, para caber na caixa de texto do jogo (0 desliga). E uma rede de
    seguranca para texto editado a mao depois do `translate`: como wrap_text() e
    idempotente, o que ja veio quebrado nao muda.
    """
    data = dat_path.read_bytes()
    script = parse(data)
    entries, meta = load_entries(texts_path)
    # LER e GRAVAR sao codificacoes diferentes e nao podem ser a mesma variavel.
    # O .DAT japones esta em cp932; a traducao PT-BR sai em utf-8. Decodificar o
    # bloco original com a codificacao de SAIDA faz o texto do .DAT "mudar" sem
    # ter mudado - e o aviso de divergencia dispara aos milhares, por engano.
    src_encoding, acertos, testados, aviso_enc = _resolve_src_encoding(
        script, entries, meta.get("encoding"))
    encoding = out_encoding or src_encoding
    nl = detect_newline(entries, None if newline == "auto" else newline)
    report = InjectReport(source=dat_path, output=out_path)
    report.src_encoding = src_encoding
    report.match_originais = (acertos, testados)
    if aviso_enc:
        report.problems.append(aviso_enc)
    if testados and acertos == 0:
        report.problems.append(
            f"ERRO: NENHUM dos {testados} textos conferidos existe neste .DAT em "
            f"codificacao nenhuma. Este .json nao corresponde a este arquivo - "
            f"confira se o --texts e a mesma arvore que voce extraiu."
        )

    for entry in entries:
        translation = entry.final
        if not translation:
            report.skipped += 1
            report.skip_sem_traducao += 1
            continue
        if not (0 <= entry.elem < len(script.elements)):
            report.problems.append(f"{entry.id}: elemento {entry.elem} fora do arquivo")
            report.skipped += 1
            report.skip_alvo += 1
            continue
        el = script.elements[entry.elem]
        if not (0 <= entry.seg < len(el.segments)) or not isinstance(el.segments[entry.seg], DataBlock):
            report.problems.append(f"{entry.id}: segmento {entry.seg} nao e um bloco de dado")
            report.skipped += 1
            report.skip_alvo += 1
            continue

        db: DataBlock = el.segments[entry.seg]
        current = decode_block(db.content, src_encoding)
        if entry.original and current is not None and current != entry.original:
            def _corta(t: str, n: int = 60) -> str:
                t = t.replace("\n", "\\n")
                return t if len(t) <= n else t[:n] + "..."
            msg = (f"{entry.id}: texto original divergente - "
                   f"no .DAT esta {_corta(current)!r}, "
                   f"no arquivo de traducao {_corta(entry.original)!r}")
            if strict_match:
                raise Stcm2lError(msg)
            # Divergencia significa que a entrada foi extraida de OUTRO texto:
            # o .json descreve um bloco que nao e este. Escrever assim troca uma
            # string pela traducao de outra - e como 'switch' vira 'trocar' e o
            # jogo perde a palavra-chave. O padrao e nao escrever.
            report.problems.append(
                msg + ("" if ignore_mismatch else " - NAO injetado (use --ignore-mismatch para forcar)")
            )
            report.divergentes += 1
            if not ignore_mismatch:
                report.skipped += 1
                report.skip_divergente += 1
                continue

        if translation == entry.original:
            report.skipped += 1
            report.skip_igual += 1
            continue

        # identificador alterado: o jogo procura voz, trilha e o PROXIMO SCRIPT
        # por esse nome. Trocar 'NO00_0012' por 'Nao 00_0012' faz o jogo nao
        # achar o recurso e voltar para o titulo - sintoma classico de "a intro
        # nao termina". Por isso o padrao e preservar o original.
        if entry.original.strip() and classify_text(entry.original) == "id":
            report.id_changes += 1
            report.problems.append(
                f"{entry.id}: IDENTIFICADOR alterado {entry.original!r} -> {translation!r}"
                + ("" if allow_id_change else " - mantido o original (use --allow-id-change para forcar)")
            )
            if not allow_id_change:
                report.skipped += 1
                report.skip_id += 1
                continue

        # so traducao passa pelo wrap - o texto original nunca e reescrito
        if max_line > 0 and entry.translation.strip():
            translation = wrap_text(translation, max_line, nl)

        blob = encode_text(translation, encoding, fallback=fallback)
        old_lens = {db.raw_len, db.padded_len, len(db.content)}
        old_size = db.size()
        budget = db.padded_len

        if fit and not db.fits_in(blob, budget):
            need = len(blob.rstrip(b"\x00")) + 1 if db.nul_terminated else len(blob)
            sobra = need - budget
            report.overflow += 1
            report.worst_overflow = max(report.worst_overflow, sobra)
            report.skipped += 1
            report.problems.append(
                f"{entry.id}: nao cabe - {need} bytes para {budget} disponiveis "
                f"(corte {sobra} byte(s)): {translation!r}"
            )
            continue

        db.set_content(blob, fit_to=budget if fit else None)
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

    out = build(script, relocate=relocate)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(out)
    report.layout_preserved = len(out) == len(data)

    if fit and not report.layout_preserved:
        report.problems.append(
            f"ERRO: modo --fit deveria preservar o tamanho ({len(data)} bytes) e a saida "
            f"tem {len(out)}. Nao use este arquivo no jogo."
        )

    # sanidade: o arquivo gerado tem que reabrir sem erros
    try:
        check = parse(out)
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
