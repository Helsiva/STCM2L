"""
stcm2l.diag
===========

Diagnostico de "0 textos".

Responde a pergunta central: o texto EXISTE no arquivo e o parser nao esta
vendo, ou o arquivo realmente nao tem dialogo?

Tres relatorios:

1. mapa do arquivo  - onde comeca/termina cada regiao (codigo, dados, exports)
2. blocos de dado   - todos, com o MOTIVO de cada recusa da heuristica de texto
3. varredura crua   - procura strings cp932/utf-16 no arquivo INTEIRO, ignorando
                      a estrutura, e diz em que regiao cada string caiu
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

from .core import DATA_HEADER_SIZE, DataBlock, Script, parse
from .textio import CANDIDATE_ENCODINGS, decode_block, detect_encoding, looks_like_text

_ALLOWED_CTRL = {0x09, 0x0A, 0x0D}


# ---------------------------------------------------------------------------
# Mapa de regioes (offsets ORIGINAIS)
# ---------------------------------------------------------------------------

def region_map(script: Script) -> list[tuple[int, int, str]]:
    """Lista (inicio, fim, rotulo) de todas as regioes do arquivo original."""
    regions: list[tuple[int, int, str]] = [(0, 0x30, "cabecalho")]
    for ei, el in enumerate(script.elements):
        pos = el.offset
        if el.kind == "action":
            hdr = el.header_size()
            regions.append((pos, pos + hdr, f"acao[{ei}] op=0x{el.opcode:X} cabecalho+params"))
            pos += hdr
        for si, seg in enumerate(el.segments):
            if isinstance(seg, DataBlock):
                regions.append((pos, pos + DATA_HEADER_SIZE, f"bloco[{ei}/{si}] cabecalho"))
                regions.append((pos + DATA_HEADER_SIZE, pos + seg.size(),
                                f"bloco[{ei}/{si}] PAYLOAD ({seg.raw_len}B uteis)"))
            else:
                regions.append((pos, pos + seg.size(), f"cru[{ei}/{si}] {seg.size()}B"))
            pos += seg.size()
    if script.exports:
        start = script.header.export_offset
        regions.append((start, start + len(script.exports) * 0x28,
                        f"tabela de exports ({len(script.exports)})"))
    if script.tail:
        regions.append((script.source_size - len(script.tail), script.source_size,
                        f"cauda ({len(script.tail)}B)"))
    return sorted(regions)


def where(regions: list[tuple[int, int, str]], off: int) -> str:
    for start, end, label in regions:
        if start <= off < end:
            return label
    return "FORA de qualquer regiao mapeada"


# ---------------------------------------------------------------------------
# Motivo de recusa da heuristica
# ---------------------------------------------------------------------------

def text_problem(text: str) -> str | None:
    """Primeiro caractere que faz looks_like_text() recusar o bloco. None = ok."""
    if not text:
        return "vazio"
    for i, ch in enumerate(text):
        cp = ord(ch)
        if cp in _ALLOWED_CTRL:
            continue
        if cp < 0x20 or cp == 0x7F:
            return f"byte de controle 0x{cp:02X} na posicao {i}"
        if unicodedata.category(ch) in ("Cc", "Cs", "Co", "Cn"):
            return f"caractere nao atribuido U+{cp:04X} na posicao {i}"
    return None


# ---------------------------------------------------------------------------
# Varredura crua de strings
# ---------------------------------------------------------------------------

def _cp932_char(data: bytes, i: int) -> tuple[int, bool] | None:
    """(tamanho, e_japones) do caractere cp932 em `i`, ou None se nao for texto."""
    b = data[i]
    if b in (0x09, 0x0A, 0x0D) or 0x20 <= b <= 0x7E:
        return 1, False
    if 0xA1 <= b <= 0xDF:                      # katakana meia-largura
        return 1, True
    if (0x81 <= b <= 0x9F or 0xE0 <= b <= 0xFC) and i + 1 < len(data):
        t = data[i + 1]
        if 0x40 <= t <= 0xFC and t != 0x7F:
            try:
                data[i:i + 2].decode("cp932")
            except UnicodeDecodeError:
                return None
            return 2, True
    return None


def scan_cp932(data: bytes, min_chars: int = 4) -> list[tuple[int, str, bool]]:
    """Acha runs de texto cp932. Retorna (offset, texto, tem_japones)."""
    out: list[tuple[int, str, bool]] = []
    i = 0
    while i < len(data):
        res = _cp932_char(data, i)
        if res is None:
            i += 1
            continue
        start = i
        jp = False
        while i < len(data):
            res = _cp932_char(data, i)
            if res is None:
                break
            size, is_jp = res
            jp = jp or is_jp
            i += size
        run = data[start:i]
        text = run.decode("cp932", errors="replace")
        if len(text) >= min_chars and (jp or len(text) >= 8):
            out.append((start, text, jp))
    return out


def scan_utf16le(data: bytes, min_chars: int = 4) -> list[tuple[int, str, bool]]:
    """Mesma ideia para UTF-16LE (usado por varios ports de Vita)."""
    def ok(cu: int) -> tuple[bool, bool]:
        if cu in (0x09, 0x0A, 0x0D) or 0x20 <= cu <= 0x7E:
            return True, False
        if 0x3000 <= cu <= 0x9FFF or 0xFF01 <= cu <= 0xFF5E or 0x30A0 <= cu <= 0x30FF:
            return True, True
        return False, False

    out: list[tuple[int, str, bool]] = []
    i = 0
    while i + 1 < len(data):
        cu = data[i] | (data[i + 1] << 8)
        good, _ = ok(cu)
        if not good:
            i += 2
            continue
        start = i
        jp = False
        chars: list[str] = []
        while i + 1 < len(data):
            cu = data[i] | (data[i + 1] << 8)
            good, is_jp = ok(cu)
            if not good:
                break
            # um par de bytes ASCII imprimiveis (ex.: "CO" -> U+4F43) vira um
            # kanji plausivel por acidente; so contam como japones os pares que
            # tem pelo menos um byte fora da faixa ASCII imprimivel
            if is_jp and not (0x20 <= data[i] <= 0x7E and 0x20 <= data[i + 1] <= 0x7E):
                jp = True
            chars.append(chr(cu))
            i += 2
        text = "".join(chars)
        if len(text) >= min_chars and jp:
            out.append((start, text, jp))
    return out


# ---------------------------------------------------------------------------
# Hexdump anotado
# ---------------------------------------------------------------------------

def _cp932_row(chunk: bytes) -> str:
    """Renderiza 16 bytes como texto cp932 (pontos onde nao for imprimivel)."""
    try:
        txt = chunk.decode("cp932")
    except UnicodeDecodeError:
        txt = chunk.decode("cp932", errors="replace")
    out = []
    for ch in txt:
        cp = ord(ch)
        out.append(ch if (0x20 <= cp <= 0x7E or cp > 0xFF) else ".")
    return "".join(out)


def hexdump(data: bytes, start: int, length: int) -> None:
    """Hexdump com o valor uint32 de cada word - o que revela ponteiros/tamanhos."""
    start = max(0, start & ~0xF)
    end = min(len(data), start + length)
    for off in range(start, end, 16):
        row = data[off:off + 16]
        hexpart = " ".join(f"{b:02X}" for b in row).ljust(47)
        words = " ".join(f"{int.from_bytes(row[i:i+4],'little'):08X}"
                         for i in range(0, len(row) - 3, 4))
        print(f"  {off:06X}  {hexpart}  | {words} | {_cp932_row(row)}")


# ---------------------------------------------------------------------------
# Quem aponta para as strings?
# ---------------------------------------------------------------------------

def pointer_analysis(data: bytes, hits: list[tuple[int, str, bool]],
                     regions: list[tuple[int, int, str]], limit: int = 8) -> None:
    """
    Procura, no arquivo inteiro, uint32 que valham o offset de cada string.

    Se as strings sao alcancadas por ponteiro, da para extrair o texto seguindo
    os ponteiros, sem depender de reconhecer a estrutura do pool.
    """
    index: dict[int, list[int]] = {}
    for off in range(0, len(data) - 3, 4):
        index.setdefault(int.from_bytes(data[off:off + 4], "little"), []).append(off)

    exatos: list[tuple[int, int]] = []      # (offset da string, offset do ponteiro)
    cabecalho: list[tuple[int, int, int]] = []   # (string, delta, ponteiro)
    orfas = 0
    for soff, _, _ in hits:
        if soff in index:
            exatos.append((soff, index[soff][0]))
            continue
        achou = False
        for delta in (4, 8, 12, 16, 20, 24, 32):
            if soff - delta in index:
                cabecalho.append((soff, delta, index[soff - delta][0]))
                achou = True
                break
        if not achou:
            orfas += 1

    total = len(hits)
    print(f"\n-- quem aponta para as {total} strings japonesas --")
    print(f"  ponteiro exato para o inicio ....... {len(exatos)}")
    print(f"  ponteiro para inicio-N (cabecalho) . {len(cabecalho)}")
    print(f"  sem nenhum ponteiro ................ {orfas}")
    for soff, poff in exatos[:limit]:
        print(f"  string 0x{soff:06X} <- ponteiro em 0x{poff:06X} ({where(regions, poff)})")
    deltas: dict[int, int] = {}
    for _, delta, _ in cabecalho:
        deltas[delta] = deltas.get(delta, 0) + 1
    if deltas:
        print("  distancia string-ponteiro mais comum: " +
              ", ".join(f"-{d}B x{n}" for d, n in sorted(deltas.items(), key=lambda x: -x[1])))
        for soff, delta, poff in cabecalho[:limit]:
            print(f"  string 0x{soff:06X} (inicio-{delta}) <- ponteiro em 0x{poff:06X} "
                  f"({where(regions, poff)})")


# ---------------------------------------------------------------------------
# Relatorio
# ---------------------------------------------------------------------------

def _preview(text: str, width: int = 60) -> str:
    flat = text.replace("\n", "\\n").replace("\r", "\\r")
    return flat if len(flat) <= width else flat[:width] + "..."


def diagnose(path: Path, forced_encoding: str | None = None,
             limit: int = 25, min_chars: int = 4) -> None:
    data = path.read_bytes()
    script = parse(data)
    encoding = detect_encoding(script, forced_encoding)
    regions = region_map(script)
    blocks = list(script.iter_data_blocks())

    print(f"\n== {path} ==")
    print(f"  tamanho .......... {len(data)} bytes")
    print(f"  magic ............ {script.header.magic_text!r}")
    print(f"  elementos ........ {len(script.elements)}  "
          f"(acoes: {sum(1 for e in script.elements if e.kind == 'action')})")
    print(f"  blocos de dado ... {len(blocks)}")
    print(f"  encoding detect .. {encoding}")
    print(f"  export_offset .... 0x{script.header.export_offset:06X} "
          f"({script.header.export_count} exports)")
    print(f"  collection_link .. 0x{script.header.collection_link:06X}")
    for tag in (b"CODE_START_", b"CODE_END_", b"GLOBAL_DATA"):
        pos = data.find(tag)
        marcas = []
        while pos != -1:
            marcas.append(f"0x{pos:06X}")
            pos = data.find(tag, pos + 1)
        print(f"  {tag.decode():<12} .. {', '.join(marcas) if marcas else 'AUSENTE'}")
    for w in script.warnings:
        print(f"  ! aviso: {w}")

    # -- 1) blocos de dado --------------------------------------------------
    print(f"\n-- blocos de dado (ate {limit}) --")
    if not blocks:
        print("  NENHUM. O parser nao encontrou nenhum bloco de dado neste arquivo;")
        print("  por isso 'extract' devolve 0. Veja a varredura crua abaixo.")
    aceitos = 0
    for n, (ei, si, db) in enumerate(blocks):
        txt = decode_block(db.content, encoding)
        if txt is not None and looks_like_text(txt):
            aceitos += 1
        if n >= limit:
            continue
        motivo = "OK (vira entrada de traducao)"
        if txt is None:
            motivo = f"nao decodifica em {encoding}"
        else:
            prob = text_problem(txt)
            if prob:
                motivo = f"recusado: {prob}"
        alt = ""
        if txt is None or not looks_like_text(txt or ""):
            for enc in CANDIDATE_ENCODINGS:
                cand = decode_block(db.content, enc)
                if cand and looks_like_text(cand):
                    alt = f"  -> mas decodifica em {enc}: {_preview(cand, 40)!r}"
                    break
        head = db.content[:16].hex(" ")
        print(f"  [{ei}/{si}] {db.raw_len:5d}B  {motivo}")
        print(f"        hex: {head}")
        if txt:
            print(f"        {encoding}: {_preview(txt)!r}")
        if alt:
            print(f"      {alt}")
    if len(blocks) > limit:
        print(f"  ... e mais {len(blocks) - limit} blocos")
    print(f"  aceitos pela heuristica: {aceitos}/{len(blocks)}")

    # -- 2) varredura crua --------------------------------------------------
    achados: dict[str, list[tuple[int, str, bool]]] = {
        "cp932 (Shift-JIS)": scan_cp932(data, min_chars),
        "utf-16le": scan_utf16le(data, min_chars),
    }
    jp_total = 0
    for nome, hits in achados.items():
        jp_hits = [h for h in hits if h[2]]
        jp_total += len(jp_hits)
        print(f"\n-- varredura crua {nome}: {len(hits)} strings "
              f"({len(jp_hits)} com japones) --")
        mostrar = jp_hits or hits
        for off, text, _ in mostrar[:limit]:
            print(f"  0x{off:06X}  {where(regions, off)}")
            print(f"            {_preview(text)!r}")
        if len(mostrar) > limit:
            print(f"  ... e mais {len(mostrar) - limit} strings")

    # -- 3) de onde as strings sao referenciadas -----------------------------
    jp_cp = [h for h in achados["cp932 (Shift-JIS)"] if h[2]]
    if jp_cp:
        pointer_analysis(data, jp_cp, regions, limit=min(limit, 8))
        print(f"\n-- hexdump ao redor das {min(3, len(jp_cp))} primeiras strings --")
        for soff, text, _ in jp_cp[:3]:
            print(f"\n  ...string em 0x{soff:06X}: {_preview(text, 40)!r}")
            hexdump(data, soff - 64, 64 + 96)

    # -- 4) veredito --------------------------------------------------------
    print("\n-- veredito --")
    if aceitos:
        print(f"  {aceitos} blocos passam pela heuristica e DEVERIAM sair no extract.")
        print("  Se 'extract' devolveu 0, o --encoding da linha de comando esta errado.")
    elif jp_total:
        print("  HA texto japones no arquivo, mas NENHUM bloco passou pela heuristica.")
        print("  Olhe a coluna de regiao das strings acima:")
        print("   - 'bloco[..] PAYLOAD' -> problema so de encoding/heuristica;")
        print("     rode 'extract --all-blocks' (ou --encoding cp932) que sai tudo.")
        print("   - 'cru[..]' ou 'FORA'  -> o texto esta fora dos blocos de dado que o")
        print("     parser reconhece; me mande este relatorio que eu ajusto o parser.")
    else:
        print("  Nenhuma string japonesa neste arquivo.")
        if blocks:
            print(f"  Ele tem {len(blocks)} blocos de dado, mas so com nomes/flags ASCII")
            print("  (chamadas de voz, labels de roteiro).")
        print("  O dialogo deve estar em OUTRO arquivo do CPK - procure fora de")
        print("  1-scenario, ou nos arquivos sem o sufixo _sub.")
