"""
stcm2l.compare
==============

Compara o .DAT original com o .DAT injetado e diz **o que mudou alem do texto**.

Serve para responder a pergunta que o `verify` nao responde: o round-trip so
prova que o parser reproduz o arquivo quando nada muda de tamanho. Depois de
injetar traducao maior que o original, tudo se move, e a ferramenta precisa
adivinhar quais words do script sao ponteiro. Um imediato do jogo (numero de
flag, alvo de salto, id de voz) que por acaso valha um offset conhecido e
reescrito junto - e o roteiro passa a saltar para o lugar errado.

O `compare` lista exatamente essas reescritas, separando:

- **texto**: o que era para mudar mesmo;
- **identificador**: nome de recurso/label que mudou (o jogo perde o recurso);
- **relocacao esperada**: word que valia um endereco e passou a valer o endereco
  novo do MESMO alvo logico - correto;
- **suspeito**: word reescrito sem alvo logico correspondente, ou alterado sem
  ser ponteiro. E aqui que mora o bug de roteiro.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .core import DataBlock, RawSegment, Script, parse, slot_verdicts
from .textio import classify_text, decode_block, detect_encoding


@dataclass
class TextDiff:
    elem: int
    seg: int
    kind: str                 # classificacao do texto ORIGINAL: cjk | prose | id
    old: str
    new: str
    old_bytes: int
    new_bytes: int

    @property
    def id_like(self) -> bool:
        return self.kind == "id"


@dataclass
class WordDiff:
    elem: int
    opcode: int | None
    where: str
    off: int                  # offset do word no arquivo ORIGINAL
    old: int
    new: int
    expected: bool            # bate com a relocacao logica do mesmo alvo
    pi: int = -1              # indice do parametro (-1 = nao e parametro)
    wi: int = -1              # word dentro do parametro


@dataclass
class SlotStat:
    """
    Quantas vezes um slot (opcode, parametro, word) foi relocado, de quantas
    instancias ele tem no arquivo.

    Um slot que E ponteiro e relocado em quase toda instancia do opcode. Um
    imediato do jogo que por acaso valeu um endereco conhecido aparece uma vez
    em duzentas - e essa e a unica pista de que ele foi reescrito por engano,
    porque o mapeamento velho->novo dele "bate" igualzinho ao de um ponteiro
    legitimo.
    """
    opcode: int
    pi: int
    wi: int
    relocados: int
    instancias: int

    @property
    def razao(self) -> float:
        return self.relocados / self.instancias if self.instancias else 0.0


#: abaixo desta fracao o slot e tratado como coincidencia, nao como ponteiro
RAZAO_SUSPEITA = 0.25


@dataclass
class CompareReport:
    original: Path
    patched: Path
    size_a: int = 0
    size_b: int = 0
    elems_a: int = 0
    elems_b: int = 0
    structural: list[str] = field(default_factory=list)
    texts: list[TextDiff] = field(default_factory=list)
    words: list[WordDiff] = field(default_factory=list)
    slots: list[SlotStat] = field(default_factory=list)
    #: veredito do slot medido no ORIGINAL: ponteiro | acaso | duvidoso
    vereditos: dict[tuple[int, int, int], str] = field(default_factory=dict)
    encoding: str = "utf-8"

    @property
    def id_changes(self) -> list[TextDiff]:
        return [t for t in self.texts if t.id_like]

    @property
    def suspects(self) -> list[WordDiff]:
        return [w for w in self.words if not w.expected]

    @property
    def relocations(self) -> list[WordDiff]:
        return [w for w in self.words if w.expected]

    @property
    def isolados(self) -> list[SlotStat]:
        """
        Slots relocados raramente E que nao passaram no teste de ponteiro.

        Razao baixa sozinha nao acusa nada: um slot pode ser relocado em 5% das
        instancias simplesmente porque nas outras 95% ele guarda 0 ou um numero
        pequeno, que nem chega a ser candidato a endereco. Quem decide e o
        veredito medido no original - acertos sobre CANDIDATAS.
        """
        return [s for s in self.slots
                if 0 < s.razao < RAZAO_SUSPEITA
                and self.vereditos.get((s.opcode, s.pi, s.wi)) != "ponteiro"]

    @property
    def clean(self) -> bool:
        return (not self.structural and not self.id_changes
                and not self.suspects and not self.isolados)


def _reloc_map(a: Script, b: Script) -> dict[int, int]:
    """
    Endereco antigo -> endereco novo, derivado dos DOIS arquivos.

    O alvo logico (elemento, segmento, deslocamento) vem do mapa do original; o
    endereco novo e onde esse mesmo alvo caiu no arquivo injetado.
    """
    out: dict[int, int] = {}
    for old, tgt in a.addr_map.items():
        if not (0 <= tgt.elem < len(b.elements)):
            continue
        el = b.elements[tgt.elem]
        if tgt.seg < 0:
            out[old] = el.offset + tgt.sub
            continue
        if tgt.seg >= len(el.segments):
            continue
        out[old] = el.offset + el.segment_rel(tgt.seg) + tgt.sub
    return out


def compare(original: Path, patched: Path) -> CompareReport:
    da, db_ = original.read_bytes(), patched.read_bytes()
    a, b = parse(da), parse(db_)
    rep = CompareReport(original=original, patched=patched,
                        size_a=len(da), size_b=len(db_),
                        elems_a=len(a.elements), elems_b=len(b.elements))
    # o injetado e MISTO: fala traduzida em utf-8 convivendo com o japones que
    # nao foi tocado. Uma codificacao so nao le os dois lados, entao cada bloco
    # e tentado na codificacao do proprio arquivo e depois na do outro.
    rep.encoding = detect_encoding(a)
    encodings = [rep.encoding]
    for enc in (detect_encoding(b), "utf-8", "cp932"):
        if enc not in encodings:
            encodings.append(enc)

    def _texto(raw: bytes) -> str:
        for enc in encodings:
            txt = decode_block(raw, enc)
            if txt is not None:
                return txt
        return raw.decode(encodings[0], "replace")

    if len(a.elements) != len(b.elements):
        rep.structural.append(
            f"contagem de elementos diferente ({len(a.elements)} vs {len(b.elements)}): "
            "o injetado nao e o mesmo script re-serializado. Comparacao word a word abortada."
        )
        return rep

    reloc = _reloc_map(a, b)

    for i, (ea, eb) in enumerate(zip(a.elements, b.elements)):
        if ea.kind != eb.kind or len(ea.segments) != len(eb.segments):
            rep.structural.append(
                f"elemento {i} (0x{ea.offset:X}) mudou de forma: "
                f"{ea.kind}/{len(ea.segments)} segs -> {eb.kind}/{len(eb.segments)} segs"
            )
            continue

        # -- parametros da acao ---------------------------------------------
        if ea.kind == "action":
            if ea.opcode != eb.opcode or ea.global_call != eb.global_call:
                rep.structural.append(
                    f"elemento {i} (0x{ea.offset:X}): opcode {ea.opcode} -> {eb.opcode}"
                )
            for pi, (pa, pb) in enumerate(zip(ea.params, eb.params)):
                for wi in range(3):
                    if pa[wi] == pb[wi]:
                        continue
                    off = ea.offset + 16 + pi * 12 + wi * 4
                    rep.words.append(WordDiff(
                        elem=i, opcode=ea.opcode,
                        where=f"param {pi} word {wi}", off=off,
                        old=pa[wi], new=pb[wi],
                        expected=reloc.get(pa[wi]) == pb[wi],
                        pi=pi, wi=wi,
                    ))

        # -- segmentos -------------------------------------------------------
        rel = ea.header_size()
        for si, (sa, sb) in enumerate(zip(ea.segments, eb.segments)):
            if isinstance(sa, DataBlock) and isinstance(sb, DataBlock):
                if sa.content != sb.content:
                    told, tnew = _texto(sa.content), _texto(sb.content)
                    rep.texts.append(TextDiff(
                        elem=i, seg=si, kind=classify_text(told),
                        old=told, new=tnew,
                        old_bytes=len(sa.content), new_bytes=len(sb.content),
                    ))
            elif isinstance(sa, RawSegment) and isinstance(sb, RawSegment):
                if sa.data != sb.data:
                    if len(sa.data) != len(sb.data):
                        rep.structural.append(
                            f"elemento {i} seg {si}: trecho cru mudou de tamanho "
                            f"({len(sa.data)} -> {len(sb.data)})"
                        )
                    else:
                        for wo in range(0, len(sa.data) - 3, 4):
                            va = int.from_bytes(sa.data[wo:wo + 4], "little")
                            vb = int.from_bytes(sb.data[wo:wo + 4], "little")
                            if va == vb:
                                continue
                            rep.words.append(WordDiff(
                                elem=i, opcode=None,
                                where=f"cru seg {si} +0x{wo:X}",
                                off=ea.offset + rel + wo,
                                old=va, new=vb,
                                expected=reloc.get(va) == vb,
                            ))
            else:
                rep.structural.append(
                    f"elemento {i} seg {si}: tipo de segmento mudou "
                    f"({type(sa).__name__} -> {type(sb).__name__})"
                )
            rel += sa.size()

    # -- exports -------------------------------------------------------------
    for xi, (xa, xb) in enumerate(zip(a.exports, b.exports)):
        if xa.name != xb.name:
            rep.structural.append(f"export {xi}: nome {xa.name_text!r} -> {xb.name_text!r}")
        if xa.offset != xb.offset:
            rep.words.append(WordDiff(
                elem=-1, opcode=None, where=f"export {xi} ({xa.name_text})",
                off=a.header.export_offset + xi * 0x28 + 0x20,
                old=xa.offset, new=xb.offset,
                expected=reloc.get(xa.offset) == xb.offset,
            ))
    if a.header.collection_link != b.header.collection_link:
        rep.words.append(WordDiff(
            elem=-1, opcode=None, where="header collection_link", off=0x28,
            old=a.header.collection_link, new=b.header.collection_link,
            expected=reloc.get(a.header.collection_link) == b.header.collection_link,
        ))

    # -- consistencia por slot ------------------------------------------------
    instancias: Counter[tuple[int, int, int]] = Counter()
    for el in a.elements:
        if el.kind != "action":
            continue
        for pi in range(len(el.params)):
            for wi in range(3):
                instancias[(el.opcode, pi, wi)] += 1
    relocados: Counter[tuple[int, int, int]] = Counter()
    for w in rep.words:
        if w.opcode is not None and w.pi >= 0:
            relocados[(w.opcode, w.pi, w.wi)] += 1
    rep.vereditos = {k: v.veredito for k, v in slot_verdicts(a).items()}
    rep.slots = sorted(
        (SlotStat(op, pi, wi, n, instancias[(op, pi, wi)])
         for (op, pi, wi), n in relocados.items()),
        key=lambda s: (s.razao, -s.instancias),
    )
    return rep
