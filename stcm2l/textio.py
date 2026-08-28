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
import math
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

#: Codificacoes que RECUSAM byte invalido - decodificar com sucesso e prova de
#: que a escolha esta certa.
STRICT_ENCODINGS = ("utf-8", "cp932", "utf-16-le")

#: Codificacoes que mapeiam byte a byte e por isso NUNCA falham. Elas decodificam
#: japones em cp932 como mojibake ('発言者名' vira '”\xadŒ¾ŽÒ–¼') sem levantar
#: erro nenhum, entao vencem qualquer disputa decidida por "quantos blocos
#: decodificam". Só entram quando nenhuma estrita da conta do arquivo.
FALLBACK_ENCODINGS = ("cp1252", "latin-1")

#: fracao dos blocos que uma codificacao estrita precisa cobrir para ser aceita
#: sem consultar as de reserva
STRICT_MIN_RATIO = 0.6

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

    def pontua(enc: str) -> int:
        return sum(1 for raw in blobs
                   if (txt := decode_block(raw, enc)) is not None and looks_like_text(txt))

    def melhor(encs: tuple[str, ...]) -> tuple[str, int]:
        # empate fica com a primeira da lista - a ordem e a preferencia
        vencedora, pontos = encs[0], -1
        for enc in encs:
            p = pontua(enc)
            if p > pontos:
                vencedora, pontos = enc, p
            if p == len(blobs):
                break
        return vencedora, pontos

    # Uma estrita que cobre a maior parte do arquivo ganha de saida. Sem esse
    # corte, um punhado de blocos nao-textuais derruba a pontuacao do cp932 e o
    # latin-1 - que decodifica QUALQUER byte - leva a disputa, transformando o
    # roteiro japones inteiro em mojibake no arquivo extraido.
    enc_estrita, pontos_estritos = melhor(STRICT_ENCODINGS)
    if pontos_estritos >= len(blobs) * STRICT_MIN_RATIO:
        return enc_estrita
    enc_reserva, pontos_reserva = melhor(FALLBACK_ENCODINGS)
    return enc_reserva if pontos_reserva > pontos_estritos else enc_estrita


#: Pontuacao que o NFKD NAO decompoe e que nao existe em cp932/ascii.
#: Sem esta tabela cada uma vira '?' - e tradutor automatico usa travessao a
#: rodo em dialogo, entao o roteiro inteiro sairia salpicado de '?'.
_PONTUACAO_LATINA = {
    "\u2014": "-",    # — travessao
    "\u2013": "-",    # – meia-risca
    "\u2012": "-",    # ‒ risca numerica
    "\u2212": "-",    # − menos matematico
    "\u00ab": '"',    # « aspas angulares
    "\u00bb": '"',    # »
    "\u2039": "'",    # ‹
    "\u203a": "'",    # ›
    "\u2022": "*",    # • marcador
    "\u00a0": " ",    # espaco nao separavel
    "\u20ac": "EUR",  # €
}
_TABELA_PONTUACAO = str.maketrans(_PONTUACAO_LATINA)


def encoding_report(script: Script, amostras: int = 3
                    ) -> tuple[list[tuple[str, int]], int, list[dict[str, str]]]:
    """
    Quanto cada codificacao candidata cobre do arquivo, e a MESMA frase lida por
    cada uma.

    A pontuacao sozinha engana: cp1252 e latin-1 nunca falham, entao empatam ou
    ganham de uma estrita mesmo transformando o roteiro em mojibake. Ver o texto
    decodificado lado a lado resolve na hora - '\u767a\u8a00\u8005\u540d' contra
    '\u201d\xad\u0152\xbe\u017d\xd2\u2013\xbc' nao deixa duvida sobre qual esta certa.
    """
    blobs = [db.content for _, _, db in script.iter_data_blocks() if db.content]
    placar: list[tuple[str, int]] = []
    for enc in CANDIDATE_ENCODINGS:
        n = sum(1 for raw in blobs
                if (txt := decode_block(raw, enc)) is not None and looks_like_text(txt))
        placar.append((enc, n))

    # amostra: os blocos mais longos, que e onde mojibake fica obvio
    exemplos: list[dict[str, str]] = []
    for raw in sorted(blobs, key=len, reverse=True)[:amostras]:
        linha = {}
        for enc in CANDIDATE_ENCODINGS:
            txt = decode_block(raw, enc)
            linha[enc] = txt if txt is not None else "(nao decodifica)"
        exemplos.append(linha)
    return placar, len(blobs), exemplos


def encode_text(text: str, encoding: str, fallback: str = "strict") -> bytes:
    """
    Codifica o texto traduzido.

    fallback="strict"   : erro se algum caractere nao couber (recomendado, avisa cedo)
    fallback="ascii"    : remove acentos (ç->c, á->a) e dobra a pontuacao
                          tipografica (— -> -, « -> ") antes de codificar
    fallback="replace"  : substitui o impossivel por '?'
    """
    try:
        return text.encode(encoding)
    except UnicodeEncodeError:
        if fallback == "ascii":
            folded = text.translate(_TABELA_PONTUACAO)
            folded = unicodedata.normalize("NFKD", folded)
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
# Fala x identificador da engine
# ---------------------------------------------------------------------------

#: kana + kanji + katakana de meia largura
CJK_RE = re.compile(r"[\u3040-\u30FF\u3400-\u4DBF\u4E00-\u9FFF\uFF66-\uFF9D]")

#: nada de espaco, so caracteres de identificador/caminho
ID_RE = re.compile(r"^[A-Za-z0-9_\-./\\]+$")

#: extensoes de recurso que aparecem soltas no script
ASSET_RE = re.compile(r"\.(at9|ogg|wav|mp3|png|dds|tga|gxt|dat|bin|txt|scr)$", re.I)


def classify_text(text: str) -> str:
    """
    "cjk"   - tem kana/kanji: fala japonesa
    "prose" - texto latino com cara de frase: fala em ingles/portugues
    "id"    - identificador da engine (ID de voz, nome de arquivo, flag, label)
              e tambem palavra-chave solta: token unico sem espaco e sem
              pontuacao de fim de frase ('switch', 'flag', 'r')

    Serve para nao mandar `NO00_0012` nem `bgm_theme_01.at9` para o tradutor:
    traduzir isso quebra o jogo, que deixa de achar o recurso.
    """
    if CJK_RE.search(text):
        return "cjk"
    core = _PH_RE.sub("", protect_tags(text)[0]).strip()
    if not core:
        return "id"                       # so marcadores da engine
    if not ID_RE.match(core):
        return "prose"                    # tem espaco ou pontuacao de frase
    if ASSET_RE.search(core) or "_" in core or any(c.isdigit() for c in core):
        return "id"
    # Daqui para baixo e um token unico, sem espaco nenhum. Num script com fala
    # em ingles isso quase nunca e dialogo e quase sempre e palavra-chave da
    # engine: 'switch', 'flag', 'jump', 'end'. Traduzir uma dessas ('switch' ->
    # 'trocar') faz o jogo perder a palavra e parar de andar, enquanto deixar uma
    # fala de uma palavra so em ingles e cosmetico - o erro barato e este.
    # Fala de uma palavra so vem pontuada ('Yes.', 'Ouch!'), e isso a salva.
    if len(core) > 1 and core[-1] in ".!?\u2026":
        return "prose"
    return "id"


# ---------------------------------------------------------------------------
# Quebra de linha (caixa de texto do jogo)
# ---------------------------------------------------------------------------

#: as duas formas de quebra que aparecem dentro do payload de texto do STCM2L
NEWLINE_LF = "\n"          # o byte 0x0A embutido no texto
NEWLINE_LITERAL = "\\n"    # barra-invertida + n, lidos pela engine

#: limite padrao de largura da caixa de texto, em colunas
MAX_LINE_DEFAULT = 50

#: quantas linhas a caixa de texto do jogo aguenta antes de cortar o resto
MAX_LINES_DEFAULT = 3

#: A quebra por palavra desperdica o fim de cada linha - quase nunca a ultima
#: palavra termina exatamente na coluna limite. Pedir `max_line * max_lines` a
#: um tradutor ou a um modelo devolve texto que fecha na conta e estoura na
#: quebra, entao o alvo declarado e uma fracao disso.
BUDGET_SLACK = 0.9

#: folga sobre a largura do original. Portugues e estruturalmente mais comprido
#: que japones, e exigir paridade exata espremeria toda fala.
FOLGA_DEFAULT = 0.15

#: percentil usado como PISO do orcamento. Ver `piso_do_lote`.
PISO_PERCENTIL = 90

#: quebras ja presentes no texto (o grupo captura para preservar a forma original)
_SPLIT_NEWLINE_RE = re.compile(r"(\\n|\r\n|\n|\r)")

_ESPACOS_RE = re.compile(r"(\s+)")


def line_count(text: str) -> int:
    """
    Quantas linhas o texto ocupa, contando as duas formas de quebra.

    `_SPLIT_NEWLINE_RE` tem grupo capturante, entao o split devolve
    [linha, quebra, linha, quebra, linha]: os separadores caem nos indices
    impares e o numero de linhas e o de posicoes pares.
    """
    if not text:
        return 0
    return (len(_SPLIT_NEWLINE_RE.split(text)) + 1) // 2


def box_budget(max_line: int = MAX_LINE_DEFAULT,
               max_lines: int = MAX_LINES_DEFAULT) -> int:
    """Alvo em colunas visiveis para uma fala caber na caixa. Ver BUDGET_SLACK."""
    if max_line <= 0 or max_lines <= 0:
        return 0
    return max(1, int(max_line * max_lines * BUDGET_SLACK))


def box_overflow(text: str, max_line: int = MAX_LINE_DEFAULT,
                 max_lines: int = MAX_LINES_DEFAULT,
                 newline: str = NEWLINE_LF) -> int:
    """
    Quantas linhas ALEM do limite a fala ocupa depois de quebrada. 0 = cabe.

    Mede o texto ja quebrado, nao o cru: e a quebra que decide quantas linhas
    saem. Como `wrap_text` e idempotente, chamar aqui e seguro mesmo em texto
    que ja veio quebrado.
    """
    if max_lines <= 0 or not text.strip():
        return 0
    return max(0, line_count(wrap_text(text, max_line, newline)) - max_lines)


def visible_width(text: str) -> int:
    """
    Largura aproximada do texto na tela, em colunas.

    Marcadores ja trocados por placeholders (protect_tags) nao ocupam coluna
    nenhuma - o jogo nao os desenha. Kana/kanji ocupam duas.

    ⚠ Espera texto JA PROTEGIDO e SEM quebras: ela desconta so os placeholders,
    entao um `#Name[2]` cru vale 8 colunas e cada `\n` vale 1. Para medir uma
    fala qualquer do pipeline, use `display_width`.
    """
    total = 0
    for ch in _PH_RE.sub("", text):
        total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return total


def display_width(text: str) -> int:
    """
    Colunas visiveis de um texto CRU: marcador e quebra nao contam.

    E a forma segura de medir qualquer fala do pipeline - crua, quebrada ou nao.
    Contar a quebra como coluna faria a mesma fala medir diferente antes e depois
    do `wrap_text`, e uma fala ja encurtada voltaria a ser candidata so por ter
    ganhado quebras.
    """
    if not text:
        return 0
    protegido = protect_tags(text)[0]
    segmentos = _SPLIT_NEWLINE_RE.split(protegido)[::2]   # impares sao as quebras
    return sum(visible_width(seg) for seg in segmentos)


def piso_do_lote(originais: Iterable[str],
                 percentil: int = PISO_PERCENTIL) -> int:
    """
    A largura que o jogo comprovadamente exibiu neste lote de falas.

    O percentil alto, e nao o maximo, para nao deixar uma fala anomala (que no
    jogo talvez ja cortasse) puxar o piso para cima sozinha.

    ⚠ Passe SO texto de fala. Identificador (`NO00_0012`, nome de arquivo) e
    curto e numeroso - num script tipico ele e a maioria das entradas - e
    afunda o percentil, apertando o orcamento de todo mundo. `originais_de_fala`
    faz esse filtro.
    """
    larguras = sorted(display_width(t) for t in originais if t and t.strip())
    if not larguras:
        return 0
    # posto mais proximo (ceil), nao truncamento: com `n * p // 100` o P90 de 10
    # itens cai no indice 9, que e o MAXIMO - justo o outlier que o percentil
    # existe para excluir
    i = max(0, math.ceil(len(larguras) * percentil / 100) - 1)
    return larguras[min(i, len(larguras) - 1)]


#: abaixo disto nao e fala em idioma nenhum - e fragmento que o parser recolheu
#: (um traco, um '<', um '}'). Medir a caixa por eles afunda a distribuicao.
MIN_COLUNAS_FALA = 4


def originais_de_fala(entries: Iterable[Any],
                      min_colunas: int = MIN_COLUNAS_FALA) -> list[str]:
    """
    Os originais que sao FALA - a populacao certa para medir a caixa.

    Dois filtros, e os dois vieram de dado real:

    1. **fragmento fora.** Num roteiro de 279 arquivos, 51056 "originais" eram
       um caractere solto ('-', '<', '}'). Nao sao fala em idioma nenhum, e
       entravam na conta arrastando a mediana para baixo.
    2. **so o script dominante.** Sobrando japones e latim, vence o que tiver
       mais falas: num jogo japones o latim restante e palavra de engine e nome
       de recurso, e vice-versa. Assim a medida da caixa sai do texto que o jogo
       realmente desenhou como dialogo, sem precisar declarar o idioma.
    """
    falas = [e.original for e in entries
             if getattr(e, "original", "").strip()
             and classify_text(e.original) != "id"
             and display_width(e.original) >= min_colunas]
    cjk = [t for t in falas if classify_text(t) == "cjk"]
    return cjk if len(cjk) * 2 >= len(falas) else falas


def entry_budget(original: str, piso: int = 0, folga: float = FOLGA_DEFAULT,
                 teto: int = 0) -> int:
    """
    Quantas colunas a traducao desta fala pode ocupar.

    A medida sai do proprio original: o jogo ja o exibiu naquela caixa, entao ele
    e a evidencia do que cabe - por fala, sem depender de medir a caixa.

    ⚠ A premissa da funcao inteira: a largura do original e limite INFERIOR da
    caixa, nao a caixa. `\u3048\uff1f` ocupa 4 colunas e isso nao quer dizer que a caixa
    tenha 4 colunas - quer dizer que ela mostra pelo menos isso. Sem o `piso`,
    toda fala curta viraria um orcamento minusculo e o encurtamento pioraria o
    texto em vez de melhorar.

    Devolve 0 quando o original nao calibra nada (vazio ou so marcadores); quem
    chama le isso como "sem base, nao mexer".
    """
    largura = display_width(original)
    if largura <= 0 and piso <= 0:
        return 0
    base = max(int(largura * (1.0 + folga)), piso)
    if teto > 0:
        base = min(base, teto)
    return max(base, 0)


def detect_newline(entries: Iterable["TextEntry"], forced: str | None = None) -> str:
    """
    Descobre como este script representa a quebra de linha.

    forced="lf" / "literal" forcam a escolha; "auto" (ou None) olha o texto
    ORIGINAL das entradas e vence a forma mais frequente. Sem nenhuma quebra no
    arquivo, o padrao e o LF real.

    ATENCAO: no formato .txt as duas formas viram LF real na leitura (unesc),
    entao a deteccao so e confiavel a partir do .json.
    """
    if forced == "lf":
        return NEWLINE_LF
    if forced == "literal":
        return NEWLINE_LITERAL
    literal = real = 0
    for e in entries:
        original = getattr(e, "original", "") or ""
        literal += original.count(NEWLINE_LITERAL)
        real += original.count(NEWLINE_LF)
    return NEWLINE_LITERAL if literal > real else NEWLINE_LF


def _wrap_segment(segment: str, max_line: int, newline: str) -> str:
    """Quebra um trecho que ja nao contem nenhuma quebra de linha."""
    if not segment.strip():
        return segment

    protected, tags = protect_tags(segment)
    linhas: list[str] = []
    atual = ""
    largura = 0
    espaco = ""

    for tok in _ESPACOS_RE.split(protected):
        if not tok:
            continue
        if tok.isspace():
            espaco += tok
            continue
        w = visible_width(tok)
        gap = visible_width(espaco)
        if atual and largura + gap + w > max_line:
            linhas.append(atual)
            atual, largura = tok, w      # o espaco do ponto de quebra e consumido
        else:
            atual += espaco + tok
            largura += gap + w
        espaco = ""

    atual += espaco                      # espaco no fim do trecho: preserva
    linhas.append(atual)
    restaurado, _ok = restore_tags(newline.join(linhas), tags)
    return restaurado


def wrap_text(text: str, max_line: int = MAX_LINE_DEFAULT,
              newline: str = NEWLINE_LF) -> str:
    """
    Quebra a fala a cada `max_line` colunas visiveis para caber na caixa de texto.

    - identificadores da engine (`NO00_0012`, `bgm.at9`) saem INTACTOS: quebrar
      um deles faz o jogo perder o recurso;
    - marcadores (`#Name[2]`, `{var}`, `%VAR%`, `\\n`) nunca sao partidos ao meio
      nem contam na largura, porque nao aparecem na tela;
    - quebras que o texto ja tinha sao preservadas na forma em que estavam;
    - uma palavra sozinha maior que o limite estoura a linha em vez de ser
      partida no meio.

    max_line <= 0 desliga a quebra. A funcao e idempotente.
    """
    if max_line <= 0 or not text.strip():
        return text
    if classify_text(text) == "id":
        return text

    pedacos = _SPLIT_NEWLINE_RE.split(text)
    return "".join(
        pedaco if i % 2 else _wrap_segment(pedaco, max_line, newline)
        for i, pedaco in enumerate(pedacos)
    )



def wrap_entries(entries: Iterable["TextEntry"], max_line: int = MAX_LINE_DEFAULT,
                 newline: str | None = None, forced: str | None = None) -> int:
    """
    Aplica wrap_text() no campo `translation` das entradas ja traduzidas.

    O texto ORIGINAL nunca e tocado: entrada sem traducao fica exatamente como
    esta. `newline` fixa a forma da quebra; sem ele, detect_newline() decide a
    partir dos originais (respeitando `forced`).

    Retorna quantas entradas ganharam pelo menos uma quebra.
    """
    itens = list(entries)
    if max_line <= 0:
        return 0
    nl = newline or detect_newline(itens, forced)
    mudou = 0
    for e in itens:
        if not e.translation.strip():
            continue
        novo = wrap_text(e.translation, max_line, nl)
        if novo != e.translation:
            e.translation = novo
            mudou += 1
    return mudou


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
