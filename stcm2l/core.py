"""
stcm2l.core
===========

Nucleo de baixo nivel da engine STCM2L (Otomate / Rejet - PS Vita, PSP, PC).

Layout implementado
-------------------

    +0x00  char[0x20]  magic  ("STCM2L ...", texto varia por titulo)
    +0x20  uint32      export_offset      (offset absoluto da tabela de exports)
    +0x24  uint32      export_count
    +0x28  uint32      collection_link    (offset absoluto do marcador GLOBAL_DATA)
    +0x2C  uint32      unknown/padding
    +0x30  ...         corpo: CODE_START_ / acoes / CODE_END_ / GLOBAL_DATA / dados
    ...              tabela de exports (export_count * 0x28)
    ...              cauda (tail) preservada byte a byte

Acao (opcode):

    uint32 global_call        (0 ou 1)
    uint32 opcode
    uint32 nparams
    uint32 length             (tamanho TOTAL da acao, incluindo este cabecalho)
    param[nparams]            (3 * uint32 cada)
    extra[]                   (blocos de dado / texto)

Bloco de dado (data block):

    uint32 f0                 (normalmente 0)
    uint32 f1                 (normalmente 1)
    uint32 padded_len         (multiplo de 4)
    uint32 raw_len            (tamanho util; padded_len - raw_len <= 4)
    uint8  payload[padded_len]  (padding sempre 0x00)

Estrategia de robustez
----------------------
O parser NAO assume que todo o corpo e composto por acoes. Ele tenta ler uma
acao valida na posicao atual; se falhar, acumula os bytes num "chunk cru"
(marcadores CODE_START_, CODE_END_, GLOBAL_DATA, padding, tabelas proprietarias)
avancando de 4 em 4 bytes ate reencontrar uma acao valida. Chunks crus tambem
sao varridos em busca de blocos de dado, para que continuem sendo alvos
validos de ponteiro.

Toda a relocacao de ponteiros e feita por MAPA DE ENDERECOS (offset antigo ->
elemento/segmento), nunca por aritmetica fixa. Assim o injetor tolera textos
maiores ou menores que o original sem dessincronizar o arquivo.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

# ---------------------------------------------------------------------------
# Constantes do formato
# ---------------------------------------------------------------------------

MAGIC_SIZE = 0x20
HEADER_SIZE = 0x30
EXPORT_ENTRY_SIZE = 0x28
EXPORT_NAME_SIZE = 0x20
ACTION_HEADER_SIZE = 16
PARAM_SIZE = 12
DATA_HEADER_SIZE = 16

MAX_PARAMS = 64          # sanidade: nenhuma acao real chega perto disso
MAX_DATA_LEN = 0x40000   # sanidade: bloco de dado de 256 KB e absurdo

TAG_CODE_START = b"CODE_START_"
TAG_CODE_END = b"CODE_END_"
TAG_GLOBAL_DATA = b"GLOBAL_DATA"


# ---------------------------------------------------------------------------
# Excecoes
# ---------------------------------------------------------------------------

class Stcm2lError(Exception):
    """Erro generico da ferramenta."""


class ParseError(Stcm2lError):
    """Arquivo nao pode ser interpretado como STCM2L."""


class BuildError(Stcm2lError):
    """Falha ao reserializar o arquivo."""


# ---------------------------------------------------------------------------
# Helpers binarios
# ---------------------------------------------------------------------------

def ru32(data: bytes, off: int) -> int:
    """Le um uint32 little-endian."""
    return struct.unpack_from("<I", data, off)[0]


def align4(value: int) -> int:
    return (value + 3) & ~3


# ---------------------------------------------------------------------------
# Cabecalho
# ---------------------------------------------------------------------------

@dataclass
class Header:
    magic: bytes
    export_offset: int
    export_count: int
    collection_link: int
    unknown: int

    @classmethod
    def parse(cls, data: bytes) -> "Header":
        if len(data) < HEADER_SIZE:
            raise ParseError(f"arquivo curto demais ({len(data)} bytes) para um STCM2L")
        magic = data[:MAGIC_SIZE]
        if b"STCM2" not in magic:
            raise ParseError(
                "assinatura STCM2L ausente nos primeiros 0x20 bytes "
                f"(encontrado: {magic[:16]!r}). Arquivo comprimido/criptografado?"
            )
        export_offset, export_count, collection_link, unknown = struct.unpack_from(
            "<4I", data, MAGIC_SIZE
        )
        return cls(magic, export_offset, export_count, collection_link, unknown)

    def pack(self) -> bytes:
        magic = self.magic[:MAGIC_SIZE].ljust(MAGIC_SIZE, b"\x00")
        return magic + struct.pack(
            "<4I", self.export_offset, self.export_count,
            self.collection_link, self.unknown,
        )

    @property
    def magic_text(self) -> str:
        return self.magic.split(b"\x00", 1)[0].decode("ascii", "replace")


# ---------------------------------------------------------------------------
# Segmentos (conteudo de um elemento)
# ---------------------------------------------------------------------------

@dataclass
class RawSegment:
    """Bytes preservados verbatim (marcadores, padding, tabelas desconhecidas)."""
    data: bytes

    def size(self) -> int:
        return len(self.data)

    def serialize(self) -> bytes:
        return self.data


@dataclass
class DataBlock:
    """Bloco de dado STCM2L. Pode conter texto (o alvo da traducao)."""
    f0: int
    f1: int
    raw_len: int
    payload: bytes            # ja inclui o padding
    pad_style: str = "min"    # "min" = pad minimo | "always" = sempre >= 1 byte

    # -- leitura ------------------------------------------------------------
    @property
    def padded_len(self) -> int:
        return len(self.payload)

    @property
    def raw(self) -> bytes:
        """Bytes uteis (sem padding)."""
        return self.payload[:self.raw_len]

    @property
    def nul_terminated(self) -> bool:
        return self.raw_len > 0 and self.payload[self.raw_len - 1] == 0

    @property
    def content(self) -> bytes:
        """Bytes uteis sem o terminador NUL."""
        raw = self.raw
        return raw[:-1] if self.nul_terminated else raw

    # -- escrita ------------------------------------------------------------
    def set_content(self, blob: bytes) -> None:
        """Substitui o conteudo util preservando terminador e convencao de padding."""
        if self.nul_terminated:
            blob = blob.rstrip(b"\x00") + b"\x00"
        self.raw_len = len(blob)
        if self.pad_style == "always":
            padded = (len(blob) // 4 + 1) * 4
        else:
            padded = align4(len(blob))
        self.payload = blob + b"\x00" * (padded - len(blob))

    def size(self) -> int:
        return DATA_HEADER_SIZE + len(self.payload)

    def serialize(self) -> bytes:
        return struct.pack(
            "<4I", self.f0, self.f1, len(self.payload), self.raw_len
        ) + self.payload


# ---------------------------------------------------------------------------
# Elementos do corpo
# ---------------------------------------------------------------------------

@dataclass
class Element:
    """
    Unidade do corpo do arquivo.

    kind == "action": possui cabecalho de 16 bytes + parametros + segmentos.
    kind == "raw":    apenas segmentos (bytes crus e/ou blocos de dado soltos).
    """
    offset: int                                    # offset ORIGINAL no arquivo
    kind: str                                      # "action" | "raw"
    global_call: int = 0
    opcode: int = 0
    params: list[list[int]] = field(default_factory=list)
    segments: list[Any] = field(default_factory=list)
    new_offset: int = -1                           # preenchido pelo builder
    orig_size: int = -1                            # tamanho lido do disco (nao muda)

    # -- geometria ----------------------------------------------------------
    def header_size(self) -> int:
        if self.kind != "action":
            return 0
        return ACTION_HEADER_SIZE + PARAM_SIZE * len(self.params)

    def segment_rel(self, index: int) -> int:
        """Offset (relativo ao inicio do elemento) do segmento `index`, com tamanhos ATUAIS."""
        rel = self.header_size()
        for seg in self.segments[:index]:
            rel += seg.size()
        return rel

    def size(self) -> int:
        return self.header_size() + sum(s.size() for s in self.segments)

    # -- serializacao -------------------------------------------------------
    def serialize(self) -> bytes:
        out = bytearray()
        if self.kind == "action":
            out += struct.pack(
                "<4I", self.global_call, self.opcode, len(self.params), self.size()
            )
            for p in self.params:
                out += struct.pack("<3I", p[0] & 0xFFFFFFFF, p[1] & 0xFFFFFFFF, p[2] & 0xFFFFFFFF)
        for seg in self.segments:
            out += seg.serialize()
        return bytes(out)

    def data_blocks(self) -> Iterator[tuple[int, DataBlock]]:
        for i, seg in enumerate(self.segments):
            if isinstance(seg, DataBlock):
                yield i, seg


@dataclass
class Export:
    name: bytes
    offset: int
    extra: int

    @property
    def name_text(self) -> str:
        return self.name.split(b"\x00", 1)[0].decode("ascii", "replace")

    def serialize(self) -> bytes:
        name = self.name[:EXPORT_NAME_SIZE].ljust(EXPORT_NAME_SIZE, b"\x00")
        return name + struct.pack("<2I", self.offset, self.extra)


# ---------------------------------------------------------------------------
# Ponteiros
# ---------------------------------------------------------------------------

@dataclass
class Target:
    """Destino de um ponteiro, expresso em coordenadas logicas (imune a resize)."""
    elem: int
    seg: int = -1      # -1 => inicio do elemento
    sub: int = 0       # deslocamento dentro do segmento (0 = cabecalho, 16 = payload)


@dataclass
class PtrSite:
    """Local do arquivo que armazena um ponteiro e precisa ser recalculado."""
    scope: str          # "header_link" | "export" | "element"
    index: int = -1     # indice do elemento ou do export
    seg: int = -1       # -1 => area de cabecalho/parametros do elemento
    word: int = 0       # offset em bytes dentro da area
    old: int = 0        # valor original (chave do mapa de enderecos)


# ---------------------------------------------------------------------------
# Script completo
# ---------------------------------------------------------------------------

@dataclass
class Script:
    header: Header
    elements: list[Element]
    exports: list[Export]
    tail: bytes = b""
    addr_map: dict[int, Target] = field(default_factory=dict)
    sites: list[PtrSite] = field(default_factory=list)
    source_size: int = 0
    warnings: list[str] = field(default_factory=list)

    def iter_data_blocks(self) -> Iterator[tuple[int, int, DataBlock]]:
        for ei, el in enumerate(self.elements):
            for si, db in el.data_blocks():
                yield ei, si, db

    def element_by_id(self, elem: int) -> Element:
        return self.elements[elem]


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _try_parse_data_block(buf: bytes, pos: int) -> Optional[DataBlock]:
    """Tenta ler um bloco de dado em `pos`. Retorna None se nao validar."""
    if pos + DATA_HEADER_SIZE > len(buf):
        return None
    f0, f1, padded_len, raw_len = struct.unpack_from("<4I", buf, pos)
    if f0 > 1 or f1 > 0xFFFF:
        return None
    if padded_len == 0 or padded_len % 4 or padded_len > MAX_DATA_LEN:
        return None
    if raw_len == 0 or raw_len > padded_len or padded_len - raw_len > 4:
        return None
    end = pos + DATA_HEADER_SIZE + padded_len
    if end > len(buf):
        return None
    payload = buf[pos + DATA_HEADER_SIZE:end]
    if any(payload[raw_len:]):          # padding tem que ser zerado
        return None
    pad = padded_len - raw_len
    pad_style = "always" if (raw_len % 4 == 0 and pad == 4) else "min"
    return DataBlock(f0, f1, raw_len, payload, pad_style)


def _parse_segments(buf: bytes) -> list[Any]:
    """Divide um buffer em blocos de dado reconhecidos + trechos crus."""
    segments: list[Any] = []
    pending = bytearray()
    i = 0
    while i < len(buf):
        db = _try_parse_data_block(buf, i)
        if db is not None:
            if pending:
                segments.append(RawSegment(bytes(pending)))
                pending = bytearray()
            segments.append(db)
            i += db.size()
        else:
            step = 4 if len(buf) - i >= 4 else len(buf) - i
            pending += buf[i:i + step]
            i += step
    if pending:
        segments.append(RawSegment(bytes(pending)))
    return segments


def _try_parse_action(data: bytes, pos: int, end: int) -> Optional[Element]:
    """Tenta ler uma acao em `pos`. Retorna None se o cabecalho nao for plausivel."""
    if pos + ACTION_HEADER_SIZE > end:
        return None
    global_call, opcode, nparams, length = struct.unpack_from("<4I", data, pos)
    if global_call > 1:
        return None
    if nparams > MAX_PARAMS:
        return None
    if length == 0 or length % 4:
        return None
    if length < ACTION_HEADER_SIZE + nparams * PARAM_SIZE:
        return None
    if pos + length > end:
        return None

    params: list[list[int]] = []
    off = pos + ACTION_HEADER_SIZE
    for _ in range(nparams):
        params.append(list(struct.unpack_from("<3I", data, off)))
        off += PARAM_SIZE

    extra = data[off:pos + length]
    el = Element(offset=pos, kind="action", global_call=global_call,
                 opcode=opcode, params=params, segments=_parse_segments(extra))
    if el.size() != length:  # nunca deve acontecer: seguranca contra bug de segmentacao
        return None
    return el


def _find_tag(data: bytes, tag: bytes, start: int, end: int) -> int:
    """Procura um marcador em posicao alinhada a 4 bytes. -1 se ausente."""
    pos = data.find(tag, start, end)
    while pos != -1:
        if pos % 4 == 0:
            return pos
        pos = data.find(tag, pos + 1, end)
    return -1


def _find_all_tags(data: bytes, start: int, end: int) -> list[int]:
    """Todas as posicoes (alinhadas) de marcadores conhecidos na faixa."""
    found: set[int] = set()
    for tag in (TAG_CODE_START, TAG_CODE_END, TAG_GLOBAL_DATA):
        pos = start
        while True:
            pos = _find_tag(data, tag, pos, end)
            if pos < 0:
                break
            found.add(pos)
            pos += 1
    return sorted(found)


def _raw_elements(data: bytes, start: int, end: int) -> list[Element]:
    """
    Converte uma faixa nao-executavel em elementos crus.

    O corte e feito nos marcadores conhecidos para que GLOBAL_DATA/CODE_END_
    virem inicio de elemento - e portanto alvos validos de ponteiro
    (collection_link aponta exatamente para GLOBAL_DATA).
    """
    if start >= end:
        return []
    cuts = sorted({start} | set(_find_all_tags(data, start, end)))
    elements: list[Element] = []
    for i, cut in enumerate(cuts):
        stop = cuts[i + 1] if i + 1 < len(cuts) else end
        if cut >= stop:
            continue
        elements.append(Element(offset=cut, kind="raw",
                                segments=_parse_segments(data[cut:stop])))
    return elements


def _parse_action_run(data: bytes, start: int, end: int) -> Optional[list[Element]]:
    """Le acoes consecutivas que preenchem exatamente [start, end). None se falhar."""
    elements: list[Element] = []
    pos = start
    while pos < end:
        el = _try_parse_action(data, pos, end)
        if el is None:
            return None
        elements.append(el)
        pos += el.size()
    return elements if pos == end else None


def _first_action_offset(data: bytes, tag_pos: int, limit: int) -> int:
    """Descobre onde comeca o codigo depois do marcador CODE_START_."""
    preferred = tag_pos + DATA_HEADER_SIZE      # marcador ocupa 16 bytes no formato padrao
    if preferred < limit and _try_parse_action(data, preferred, limit) is not None:
        return preferred
    pos = align4(tag_pos + len(TAG_CODE_START))
    while pos < limit:
        if _try_parse_action(data, pos, limit) is not None:
            return pos
        pos += 4
    return min(preferred, limit)


def _extra_looks_clean(el: Element) -> bool:
    """
    Filtro anti-falso-positivo usado apenas na varredura heuristica.

    Uma acao real ou nao tem conteudo extra, ou o conteudo comeca num bloco de
    dado. Um "cabecalho" que cai no meio de uma area de dados costuma produzir
    extra que comeca com bytes crus - e por isso e descartado aqui.
    """
    return not el.segments or isinstance(el.segments[0], DataBlock)


def _scan_mixed(data: bytes, start: int, end: int) -> list[Element]:
    """
    Varredura heuristica para arquivos sem os marcadores CODE_START_/CODE_END_.

    Tenta uma acao na posicao atual; senao acumula bytes crus de 4 em 4.
    Blocos de dado tem prioridade sobre acoes para reduzir falsos positivos.
    """
    elements: list[Element] = []
    pos = start
    pending = -1

    def flush(stop: int) -> None:
        nonlocal pending
        if pending >= 0:
            # mesma divisao por marcadores usada no caminho normal, para que
            # GLOBAL_DATA continue sendo um alvo exato de ponteiro
            elements.extend(_raw_elements(data, pending, stop))
            pending = -1

    while pos < end:
        db = _try_parse_data_block(data, pos) if pos + DATA_HEADER_SIZE <= end else None
        if db is not None and pos + db.size() <= end:
            if pending < 0:
                pending = pos
            pos += db.size()
            continue
        el = _try_parse_action(data, pos, end)
        if el is not None and not _extra_looks_clean(el):
            el = None            # provavel falso positivo dentro de area de dados
        if el is not None:
            flush(pos)
            elements.append(el)
            pos += el.size()
        else:
            if pending < 0:
                pending = pos
            pos += 4 if end - pos >= 4 else end - pos
    flush(end)
    return elements


def _parse_body(data: bytes, start: int, end: int, warnings: list[str]) -> list[Element]:
    """
    Divide o corpo em: prefixo cru (CODE_START_), faixa de codigo, dados globais.

    Delimitar o codigo pelos marcadores evita que blocos de dado precedidos de
    um word zerado sejam lidos como acoes fantasmas.
    """
    code_tag = _find_tag(data, TAG_CODE_START, start, end)
    end_tag = _find_tag(data, TAG_CODE_END, start, end)
    if code_tag >= 0 and end_tag > code_tag:
        code_begin = _first_action_offset(data, code_tag, end_tag)
        run = _parse_action_run(data, code_begin, end_tag)
        if run is not None:
            return (_raw_elements(data, start, code_begin) + run
                    + _raw_elements(data, end_tag, end))
        warnings.append(
            f"a faixa de codigo 0x{code_begin:X}-0x{end_tag:X} nao e composta apenas "
            "por acoes validas; usando varredura heuristica"
        )
    elif code_tag < 0:
        warnings.append("marcador CODE_START_ ausente; usando varredura heuristica")
    return _scan_mixed(data, start, end)


def parse(data: bytes) -> Script:
    """Interpreta um arquivo STCM2L completo."""
    header = Header.parse(data)
    warnings: list[str] = []

    body_end = header.export_offset
    if not (HEADER_SIZE <= body_end <= len(data)) or body_end % 4:
        warnings.append(
            f"export_offset invalido (0x{header.export_offset:X}); "
            "tratando o arquivo inteiro como corpo e ignorando a tabela de exports"
        )
        body_end = len(data)
        exports_ok = False
    else:
        exports_ok = True

    elements = _parse_body(data, HEADER_SIZE, body_end, warnings)

    # -- tabela de exports --------------------------------------------------
    exports: list[Export] = []
    tail = b""
    if exports_ok:
        need = header.export_count * EXPORT_ENTRY_SIZE
        if header.export_offset + need > len(data):
            warnings.append(
                f"tabela de exports truncada (declarados {header.export_count}); "
                "lendo apenas o que cabe no arquivo"
            )
            need = (len(data) - header.export_offset) // EXPORT_ENTRY_SIZE * EXPORT_ENTRY_SIZE
        off = header.export_offset
        for _ in range(need // EXPORT_ENTRY_SIZE):
            name = data[off:off + EXPORT_NAME_SIZE]
            e_off, e_extra = struct.unpack_from("<2I", data, off + EXPORT_NAME_SIZE)
            exports.append(Export(name, e_off, e_extra))
            off += EXPORT_ENTRY_SIZE
        tail = data[off:]

    script = Script(header=header, elements=elements, exports=exports,
                    tail=tail, source_size=len(data), warnings=warnings)
    _index(script)
    return script


def _index(script: Script) -> None:
    """Monta o mapa de enderecos e a lista de candidatos a ponteiro."""
    addr: dict[int, Target] = {}
    sites: list[PtrSite] = []

    for el in script.elements:
        if el.orig_size < 0:
            el.orig_size = el.size()

    for ei, el in enumerate(script.elements):
        addr.setdefault(el.offset, Target(ei, -1, 0))
        rel = el.header_size()
        for si, seg in enumerate(el.segments):
            addr.setdefault(el.offset + rel, Target(ei, si, 0))
            if isinstance(seg, DataBlock):
                addr.setdefault(el.offset + rel + DATA_HEADER_SIZE, Target(ei, si, DATA_HEADER_SIZE))
            rel += seg.size()

    for ei, el in enumerate(script.elements):
        if el.kind == "action":
            for pi, p in enumerate(el.params):
                base = ACTION_HEADER_SIZE + pi * PARAM_SIZE
                for wi in range(3):
                    sites.append(PtrSite("element", ei, -1, base + wi * 4, p[wi]))
        # trechos crus tambem podem guardar ponteiros (tabelas internas)
        rel = el.header_size()
        for si, seg in enumerate(el.segments):
            if isinstance(seg, RawSegment):
                for wo in range(0, len(seg.data) - 3, 4):
                    sites.append(PtrSite("element", ei, si, wo, ru32(seg.data, wo)))
            rel += seg.size()

    for xi, ex in enumerate(script.exports):
        sites.append(PtrSite("export", xi, -1, 0, ex.offset))

    sites.append(PtrSite("header_link", -1, -1, 0, script.header.collection_link))

    script.addr_map = addr
    script.sites = sites


# ---------------------------------------------------------------------------
# Builder (recalculo de ponteiros)
# ---------------------------------------------------------------------------

def build(script: Script, relocate: str = "scan") -> bytes:
    """
    Reserializa o script recalculando todos os offsets.

    relocate="scan"   : qualquer uint32 (parametros, exports, trechos crus) cujo
                        valor caia exatamente sobre um endereco conhecido e
                        tratado como ponteiro. Padrao - cobre variacoes de header.
    relocate="strict" : apenas o 1o word de cada parametro, exports e
                        collection_link sao relocados.
    """
    if relocate not in ("scan", "strict"):
        raise BuildError(f"modo de relocacao desconhecido: {relocate}")

    # 1) novo layout ---------------------------------------------------------
    cursor = HEADER_SIZE
    for el in script.elements:
        el.new_offset = cursor
        cursor += el.size()
    body_end = align4(cursor)
    pad_body = body_end - cursor

    # 2) serializa elementos -------------------------------------------------
    blobs = [bytearray(el.serialize()) for el in script.elements]

    def resolve(old: int, interior: bool = False) -> Optional[int]:
        """
        Converte um offset antigo no novo. `interior=True` habilita o fallback
        para ponteiros que sabidamente sao ponteiros (collection_link, exports)
        mas caem no MEIO de um chunk cru - chunks crus nunca mudam de tamanho,
        entao o deslocamento interno continua valido.
        """
        tgt = script.addr_map.get(old)
        if tgt is not None:
            el = script.elements[tgt.elem]
            if tgt.seg < 0:
                return el.new_offset + tgt.sub
            return el.new_offset + el.segment_rel(tgt.seg) + tgt.sub
        if not interior:
            return None
        for el in script.elements:
            if el.offset <= old < el.offset + el.orig_size:
                if el.kind == "raw":
                    return el.new_offset + (old - el.offset)
                return None
        return None

    # 3) aplica relocacao ----------------------------------------------------
    new_link = script.header.collection_link
    for site in script.sites:
        if relocate == "strict":
            if site.scope == "element" and site.seg >= 0:
                continue
            if site.scope == "element" and site.word % PARAM_SIZE != ACTION_HEADER_SIZE % PARAM_SIZE:
                # em modo estrito so o primeiro word do parametro e ponteiro
                if (site.word - ACTION_HEADER_SIZE) % PARAM_SIZE != 0:
                    continue
        new_val = resolve(site.old, interior=site.scope in ("header_link", "export"))
        if new_val is None or new_val == site.old:
            continue
        if site.scope == "header_link":
            new_link = new_val
        elif site.scope == "export":
            script.exports[site.index].offset = new_val
        else:
            el = script.elements[site.index]
            base = 0 if site.seg < 0 else el.segment_rel(site.seg)
            struct.pack_into("<I", blobs[site.index], base + site.word, new_val)

    # 4) monta o arquivo -----------------------------------------------------
    out = bytearray()
    header = Header(
        magic=script.header.magic,
        export_offset=body_end,
        export_count=len(script.exports),
        collection_link=new_link,
        unknown=script.header.unknown,
    )
    out += header.pack()
    for blob in blobs:
        out += blob
    out += b"\x00" * pad_body
    for ex in script.exports:
        out += ex.serialize()
    out += script.tail

    if len(out) < HEADER_SIZE:
        raise BuildError("saida vazia - script sem elementos")
    return bytes(out)


def roundtrip_check(data: bytes, relocate: str = "scan") -> tuple[bool, str]:
    """Le e reescreve sem alteracoes; confirma se o resultado e identico."""
    script = parse(data)
    rebuilt = build(script, relocate=relocate)
    if rebuilt == data:
        return True, "identico"
    if len(rebuilt) != len(data):
        return False, f"tamanho diferente: original {len(data)} vs reconstruido {len(rebuilt)}"
    for i, (a, b) in enumerate(zip(data, rebuilt)):
        if a != b:
            return False, f"primeira divergencia em 0x{i:X}: original 0x{a:02X} vs 0x{b:02X}"
    return False, "divergencia desconhecida"
