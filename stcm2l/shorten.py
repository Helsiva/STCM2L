"""
stcm2l.shorten
==============

Encurta a fala traduzida que nao cabe na caixa de texto do jogo.

O problema e estrutural, nao pontual: portugues e mais comprido que japones, e
a caixa aguarda um numero fixo de linhas. `--max-line` sempre respeitou a
LARGURA, mas nada no pacote contava quantas linhas sairam - uma fala que virava
cinco linhas atravessava o pipeline inteiro e so aparecia no jogo.

Cortar por regra (truncar, abreviar) resolveria o tamanho e estragaria o texto:
em visual novel o COMO se diz e metade do produto. Entao quem reescreve e um
modelo, com duas passadas:

1. **reescrever** - mesma informacao e mesma voz, com menos palavras;
2. **resumir** - so no que sobrou, aceitando perder detalhe secundario.

⚠ Quem mede se coube e esta ferramenta, nunca o modelo: ele nao conhece a regra
de quebra daqui. Toda resposta passa por `box_overflow` antes de ser aceita.
"""

from __future__ import annotations

import json
import math
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .textio import (
    _SPLIT_NEWLINE_RE,
    FOLGA_DEFAULT, MAX_LINE_DEFAULT, MAX_LINES_DEFAULT, PISO_PERCENTIL, TextEntry,
    box_budget, box_overflow, classify_text, detect_newline, display_width,
    entry_budget, line_count, originais_de_fala, piso_do_lote, protect_tags,
    restore_tags,
    visible_width, wrap_text,
)
from .translate import (
    FatalTranslationError, TranslationError, TransientTranslationError,
    _erro_de_rede, _ERROS_DE_REDE, _post, _salvar_cache, _ssl_context,
)

#: provedores de encurtamento
PROVEDORES = ("gemini", "claude")

#: modelo padrao de cada provedor. Nome de modelo muda com frequencia dos dois
#: lados - quando a API recusar, o comando lista os disponiveis em vez de deixar
#: voce adivinhar.
MODELO_PADRAO = {
    # modelo fixo em vez do alias "-latest": o alias concentra a demanda e devolve
    # 503 com frequencia. Nome que envelhece se conserta sozinho - quando a API
    # recusar, o comando lista os disponiveis.
    "gemini": "gemini-3.5-flash",
    "claude": "claude-opus-5",
}

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

#: esforco padrao. Encurtar uma fala e tarefa simples e o esforco domina o custo
#: quando sao milhares de chamadas; `--ai-effort` sobe quando o texto nao ficar bom.
ESFORCO_PADRAO = "low"

#: assinatura do que `shorten_entries` chama. Recebe os textos JA protegidos
#: (marcadores trocados por placeholders), o orcamento de cada um em colunas
#: visiveis, e se e a passada de resumo. Devolve a lista na MESMA ordem.
Chamador = Callable[[list[str], list[int], bool], list[str]]

#: uma unidade de trabalho do encurtamento: (traducao crua, orcamento em colunas)
Alvo = tuple[str, int]


# ---------------------------------------------------------------------------
# Provedor: Claude
# ---------------------------------------------------------------------------

#: Instrucoes fixas. Precisam ficar byte a byte iguais entre requisicoes, senao
#: o cache de prompt e invalidado - por isso o modo (reescrever/resumir) vai na
#: mensagem do usuario, e nao aqui.
SYSTEM_PROMPT = """\
Voce encurta falas de visual novel japonesa traduzidas para portugues do Brasil, \
para que caibam na caixa de texto do jogo.

Cada fala vem com um orcamento em colunas visiveis, medido no texto original dela. \
O orcamento VARIA de fala para fala dentro do mesmo lote: use o numero que acompanha \
cada uma, nunca o da anterior.

Regras:
- Preserve o sentido, o registro e a VOZ do personagem. Giria continua giria, \
formalidade continua formal, interjeicao continua interjeicao.
- Nao invente informacao que nao esteja na fala.
- Responda em portugues do Brasil.
- Trechos como U+27E6 0 U+27E7 sao marcadores da engine do jogo. Devolva todos, \
inalterados e na mesma ordem relativa. Eles nao ocupam espaco na tela.
- Nao adicione quebras de linha: devolva cada fala em uma linha so. A quebra e \
feita depois, por quem chamou.
- Nao coloque aspas nem comentarios em volta da fala.
- Se a fala ja couber no orcamento, devolva-a sem mudanca.

Modo "reescrever": diga a MESMA coisa com menos palavras - troque rodeio por \
frase direta, corte redundancia, prefira a palavra curta. Nao descarte informacao.

Modo "resumir": a reescrita nao bastou. Agora pode descartar detalhe secundario, \
mantendo o nucleo da fala e o tom. Continue soando como fala, nao como resumo.

Responda SOMENTE com um objeto JSON nesta forma, sem texto em volta:

{"falas": [{"i": 0, "texto": "a fala encurtada"}, {"i": 1, "texto": "..."}]}

O campo "i" repete o numero da fala do pedido. Devolva uma entrada para CADA \
fala recebida.\
"""

_SCHEMA = {
    "type": "object",
    "properties": {
        "falas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer"},
                    "texto": {"type": "string"},
                },
                "required": ["i", "texto"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["falas"],
    "additionalProperties": False,
}


def _pedido(textos: list[str], orcamentos: list[int], resumir: bool) -> str:
    """A mensagem do usuario. Igual para os dois provedores."""
    modo = "resumir" if resumir else "reescrever"
    linhas = [f"{i}. (max {orc} colunas) {txt}"
              for i, (txt, orc) in enumerate(zip(textos, orcamentos))]
    return (f"Modo: {modo}\n\n"
            "Encurte cada fala abaixo. Devolva o campo 'i' igual ao numero da fala.\n\n"
            + "\n".join(linhas))


def _cliente(api_key: str | None):
    try:
        import anthropic
    except ImportError as exc:
        raise FatalTranslationError(
            "o encurtamento por IA precisa do SDK da Anthropic: pip install anthropic"
        ) from exc
    return anthropic, (anthropic.Anthropic(api_key=api_key) if api_key
                       else anthropic.Anthropic())


def claude_encurtar(textos: list[str], orcamentos: list[int], resumir: bool = False,
                    *, modelo: str = MODELO_PADRAO["claude"],
                    api_key: str | None = None,
                    esforco: str = ESFORCO_PADRAO) -> list[str]:
    """
    Manda um lote de falas para o Claude encurtar. Devolve na mesma ordem.

    Sem `api_key`, o SDK resolve a credencial sozinho (ANTHROPIC_API_KEY,
    ANTHROPIC_AUTH_TOKEN ou o perfil do `ant auth login`).
    """
    if not textos:
        return []
    anthropic, client = _cliente(api_key)

    pedido = _pedido(textos, orcamentos, resumir)

    try:
        resposta = client.messages.create(
            model=modelo,
            max_tokens=8000,
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": pedido}],
            output_config={
                "effort": esforco,
                "format": {"type": "json_schema", "schema": _SCHEMA},
            },
        )
    except anthropic.AuthenticationError as exc:
        raise FatalTranslationError(
            "a API da Anthropic recusou a credencial. Exporte ANTHROPIC_API_KEY, "
            "use --api-key, ou rode `ant auth login`."
        ) from exc
    except anthropic.APIConnectionError as exc:
        raise TransientTranslationError(f"falha de conexao com a API: {exc}") from exc
    except anthropic.APIStatusError as exc:
        if exc.status_code in (408, 409, 429, 500, 502, 503, 504):
            raise TransientTranslationError(
                f"a API respondeu {exc.status_code}; vale repetir") from exc
        raise TranslationError(f"a API respondeu {exc.status_code}: {exc}") from exc

    bruto = ""
    for bloco in resposta.content:
        if getattr(bloco, "type", None) == "text":
            bruto = bloco.text
    return _colher(bruto, len(textos), textos)


def _colher(bruto: str, esperado: int, originais: list[str]) -> list[str]:
    """
    Extrai a lista de falas do JSON devolvido, ancorada pelo indice.

    JSON valido nao garante que o modelo devolveu TODAS as falas. O campo 'i' e
    o que permite reancorar: faltando alguma, fica o texto original daquela
    posicao em vez de deslocar o lote inteiro - um deslocamento aqui trocaria a
    fala de um personagem pela de outro.
    """
    if not bruto.strip():
        raise TranslationError("a API devolveu uma resposta sem texto")
    try:
        dados = json.loads(bruto)
    except json.JSONDecodeError as exc:
        raise TranslationError(f"a API devolveu um JSON invalido: {exc}") from exc

    por_indice: dict[int, str] = {}
    for item in dados.get("falas", []):
        try:
            por_indice[int(item["i"])] = str(item["texto"])
        except (KeyError, TypeError, ValueError):
            continue
    if not por_indice:
        raise TranslationError("a API devolveu um lote sem nenhuma fala utilizavel")
    return [por_indice.get(i, originais[i]) for i in range(esperado)]


def fazer_chamador(provedor: str = "gemini", modelo: str | None = None,
                   api_key: str | None = None,
                   esforco: str = ESFORCO_PADRAO) -> Chamador:
    """Fecha as opcoes do provedor numa funcao com a assinatura que o orquestrador usa."""
    if provedor not in PROVEDORES:
        raise FatalTranslationError(f"provedor de IA desconhecido: {provedor}")
    nome = modelo or MODELO_PADRAO[provedor]

    def chamar(textos: list[str], orcamentos: list[int], resumir: bool) -> list[str]:
        if provedor == "gemini":
            return gemini_encurtar(textos, orcamentos, resumir,
                                   modelo=nome, api_key=api_key)
        return claude_encurtar(textos, orcamentos, resumir,
                               modelo=nome, api_key=api_key, esforco=esforco)
    return chamar


# ---------------------------------------------------------------------------
# Provedor: Gemini (urllib puro, sem dependencia)
# ---------------------------------------------------------------------------

#: a forma de autenticacao que funcionou, memorizada como translate.py faz com
#: as variantes de client do gtx
_auth_ok: str | None = None


def _auth_gemini(api_key: str | None, forma: str) -> tuple[str, dict[str, str]]:
    """
    Devolve (sufixo da URL, cabecalhos) para uma das duas formas de credencial.

    Nao da para decidir pela aparencia da chave: existe credencial que nao comeca
    com "AIza" e mesmo assim so e aceita como `?key=`, e token OAuth que so e
    aceito no cabecalho. Entao as duas sao tentadas, e a que passar fica.
    """
    if not api_key:
        raise FatalTranslationError(
            "falta a credencial do Gemini: use --api-key ou exporte GEMINI_API_KEY")
    if forma == "query":
        return f"?key={urllib.parse.quote(api_key)}", {"Content-Type": "application/json"}
    return "", {"Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}"}


def _gemini_http(caminho: str, api_key: str | None, corpo: bytes | None = None,
                 timeout: int = 120) -> dict:
    """
    Chama a API tentando as duas formas de credencial, e guarda a que funcionou.

    Sem isto, uma chave valida na forma errada devolve 401 com a mensagem
    "Expected OAuth 2 access token", que manda o usuario procurar o problema no
    lugar errado.
    """
    global _auth_ok
    formas = [_auth_ok] if _auth_ok else ["query", "bearer"]
    ultimo: TranslationError | None = None
    for forma in formas:
        sufixo, headers = _auth_gemini(api_key, forma)
        url = f"{GEMINI_BASE}/{caminho}{sufixo}"
        try:
            if corpo is None:
                req = urllib.request.Request(url, headers=headers, method="GET")
                with urllib.request.urlopen(req, timeout=timeout,
                                            context=_ssl_context()) as resp:
                    dados = json.loads(resp.read().decode("utf-8"))
            else:
                dados = _post(url, corpo, headers, timeout=timeout)
        except _ERROS_DE_REDE as exc:
            erro = _erro_de_rede(exc, url)
            if "401" not in str(erro) and "403" not in str(erro):
                raise erro from exc
            ultimo = erro
            continue
        except TranslationError as exc:
            if "401" not in str(exc) and "403" not in str(exc):
                raise
            ultimo = exc
            continue
        _auth_ok = forma
        return dados
    raise FatalTranslationError(
        "a API do Gemini recusou a credencial nas duas formas (?key= e Bearer). "
        f"Confira se a chave esta valida e habilitada. Ultimo erro: {ultimo}")


def listar_modelos_gemini(api_key: str | None) -> list[str]:
    """Os modelos que a credencial enxerga. Usado para nao deixar voce adivinhar o nome."""
    dados = _gemini_http("models", api_key, timeout=30)
    nomes = []
    for m in dados.get("models", []):
        nome = str(m.get("name", "")).removeprefix("models/")
        if nome and "generateContent" in m.get("supportedGenerationMethods", []):
            nomes.append(nome)
    return sorted(nomes)


def gemini_encurtar(textos: list[str], orcamentos: list[int], resumir: bool = False,
                    *, modelo: str = MODELO_PADRAO["gemini"],
                    api_key: str | None = None) -> list[str]:
    """
    Manda um lote de falas para o Gemini encurtar. Devolve na mesma ordem.

    Vai por urllib, como os outros provedores do projeto - o encurtamento nao
    adiciona dependencia nenhuma.
    """
    if not textos:
        return []
    corpo = json.dumps({
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user",
                      "parts": [{"text": _pedido(textos, orcamentos, resumir)}]}],
        "generationConfig": {"responseMimeType": "application/json",
                             "temperature": 0.3},
    }).encode("utf-8")

    try:
        dados = _gemini_http(f"models/{modelo}:generateContent", api_key, corpo)
    except TranslationError as exc:
        # nome de modelo errado e o erro mais provavel aqui, e a mensagem crua da
        # API nao ajuda: listar o que a credencial enxerga resolve na hora
        if "404" in str(exc) or "400" in str(exc):
            try:
                disponiveis = listar_modelos_gemini(api_key)
            except TranslationError:
                raise exc
            if modelo not in disponiveis:
                raise FatalTranslationError(
                    f"o modelo '{modelo}' nao existe ou nao esta liberado para esta "
                    f"credencial. Disponiveis: {', '.join(disponiveis[:12])}"
                    + (" ..." if len(disponiveis) > 12 else "")
                ) from exc
        raise

    candidatos = dados.get("candidates") or []
    if not candidatos:
        motivo = (dados.get("promptFeedback") or {}).get("blockReason")
        raise TranslationError(
            f"o Gemini nao devolveu resposta{f' (bloqueado: {motivo})' if motivo else ''}")
    partes = ((candidatos[0].get("content") or {}).get("parts")) or []
    bruto = "".join(str(parte.get("text", "")) for parte in partes)
    return _colher(bruto, len(textos), textos)

# ---------------------------------------------------------------------------
# Orquestracao
# ---------------------------------------------------------------------------

@dataclass
class Exemplo:
    """Um antes/depois para o relatorio. Largura manda; linha vira coadjuvante."""
    id: str
    antes: str
    depois: str
    colunas_antes: int
    colunas_depois: int
    orcamento: int
    linhas_antes: int
    linhas_depois: int

    @property
    def faltam(self) -> int:
        """Colunas que ainda precisam sair. <= 0 quer dizer que coube."""
        return (self.colunas_depois - self.orcamento) if self.orcamento else 0


@dataclass
class ResumoEncurtamento:
    candidatas: int = 0
    resolvidas_1: int = 0        # couberam so com a reescrita
    resolvidas_2: int = 0        # precisaram do resumo
    restantes: int = 0           # nem o resumo resolveu - marcadas para revisao
    marcadores_perdidos: int = 0
    do_cache: int = 0
    #: de qual criterio veio cada candidata - serve para calibrar a folga
    por_largura: int = 0
    por_linhas: int = 0
    exemplos: list[Exemplo] = field(default_factory=list)

    @property
    def resolvidas(self) -> int:
        return self.resolvidas_1 + self.resolvidas_2


def _com_retry(chamar: Chamador, textos: list[str], orcamentos: list[int],
               resumir: bool, retries: int, delay: float,
               log: Callable[[str], None]) -> list[str]:
    """
    Repete o lote em erro transitorio. Modelo popular devolve 503 por demanda
    alta com frequencia, e perder o lote inteiro por isso seria desperdicio -
    ainda mais depois de ja ter pago pelos anteriores.
    """
    for tentativa in range(1, retries + 1):
        try:
            return chamar(textos, orcamentos, resumir)
        except TranslationError as exc:
            if exc.fatal or not exc.transient:
                raise
            if tentativa == retries:
                if "503" in str(exc):
                    raise TranslationError(
                        f"{exc}\n\nEste modelo esta congestionado. Tente outro com "
                        f"--ai-model (ex.: gemini-3.1-flash-lite), ou repita mais tarde."
                    ) from exc
                raise
            espera = delay * tentativa
            log(f"  tentativa {tentativa}/{retries} falhou ({exc.__class__.__name__}); "
                f"repetindo em {espera:.0f}s")
            time.sleep(espera)
    raise TranslationError("retry esgotado")


def _chave(texto: str, orcamento: int, max_line: int, max_lines: int) -> str:
    """
    O orcamento entra na chave, e nao e detalhe.

    Ele e o unico numero que aparece no pedido ao modelo, entao a resposta e
    especifica dele. Sem isso o cache serviria a versao de 46 colunas para uma
    entrada com orcamento 30 - e o erro seria silencioso, porque o valor entra
    em `melhor` ANTES da conferencia e so seria pego na passada de resumo.
    """
    return f"{max_line}x{max_lines}w{orcamento}|{texto}"


def candidatas_orcadas(entries: Iterable[TextEntry],
                       max_line: int = MAX_LINE_DEFAULT,
                       max_lines: int = MAX_LINES_DEFAULT,
                       newline: str = "auto", folga: float = FOLGA_DEFAULT,
                       percentil: int = PISO_PERCENTIL,
                       tolerancia: int = 0,
                       usar_original: bool = True,
                       piso_fixo: int = 0) -> list[tuple[TextEntry, int]]:
    """
    As entradas que estouram - por LARGURA ou por LINHAS - com o orcamento de cada.

    O orcamento de largura sai do texto original da propria fala; o piso sai do
    percentil das larguras deste lote, porque original curto nao prova caixa
    estreita. Identificador nunca entra.
    """
    itens = list(entries)
    nl = detect_newline(itens, None if newline == "auto" else newline)
    teto = box_budget(max_line, max_lines)
    # A caixa e do JOGO, nao do arquivo: o maximo de um arquivo so diz que
    # aquela cena nunca precisou de mais. `piso_fixo` deixa fixar o numero
    # medido no corpus inteiro, que e a estimativa certa da capacidade.
    if piso_fixo > 0:
        piso = piso_fixo
    else:
        piso = piso_do_lote(originais_de_fala(itens), percentil) if usar_original else 0

    out: list[tuple[TextEntry, int]] = []
    for e in itens:
        if not e.translation.strip() or classify_text(e.original) == "id":
            continue
        orc = entry_budget(e.original, piso, folga, teto) if usar_original else teto
        largura = display_width(e.translation)
        estoura_largura = bool(orc) and largura > orc + tolerancia
        estoura_linhas = box_overflow(e.translation, max_line, max_lines, nl) > 0
        if estoura_largura or estoura_linhas:
            out.append((e, orc))
    return out


def candidatas(entries: Iterable[TextEntry], max_line: int = MAX_LINE_DEFAULT,
               max_lines: int = MAX_LINES_DEFAULT,
               newline: str = "auto", **kw) -> list[TextEntry]:
    """As entradas traduzidas que estouram a caixa. Identificador nunca entra."""
    return [e for e, _ in candidatas_orcadas(entries, max_line, max_lines, newline, **kw)]


def shorten_entries(entries: list[TextEntry], chamar_modelo: Chamador, *,
                    max_line: int = MAX_LINE_DEFAULT,
                    max_lines: int = MAX_LINES_DEFAULT,
                    newline: str = "auto", batch_size: int = 25,
                    retries: int = 3, delay: float = 2.0,
                    folga: float = FOLGA_DEFAULT,
                    percentil: int = PISO_PERCENTIL,
                    tolerancia: int = 0, usar_original: bool = True,
                    piso_fixo: int = 0,
                    cache_path: Path | None = None,
                    log: Callable[[str], None] = print) -> ResumoEncurtamento:
    """
    Encurta in-place o campo `translation` do que nao cabe na caixa.

    `chamar_modelo` e injetado para que o fluxo inteiro seja testavel sem rede -
    a mesma razao pela qual `_gtx_normaliza` foi extraida em translate.py.
    """
    rep = ResumoEncurtamento()
    nl = detect_newline(entries, None if newline == "auto" else newline)
    alvos = candidatas_orcadas(entries, max_line, max_lines, newline,
                               folga, percentil, tolerancia, usar_original,
                               piso_fixo)
    rep.candidatas = len(alvos)
    if not alvos:
        return rep
    for e, orc in alvos:
        if orc and display_width(e.translation) > orc + tolerancia:
            rep.por_largura += 1
        else:
            rep.por_linhas += 1

    cache: dict[str, str] = {}
    if cache_path and cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))

    # Deduplica pelo PAR (texto, orcamento), nao so pelo texto: a mesma traducao
    # pode servir a originais de larguras diferentes, e servir a resposta de um
    # orcamento ao outro deixaria o segundo estourando. Deduplicar pelo MENOR
    # orcamento economizaria requisicoes, mas faria o orcamento depender de quais
    # arquivos entraram na rodada - e a chave do cache junto.
    unicos: list[Alvo] = []
    vistos: set[Alvo] = set()
    for e, orc in alvos:
        chave = (e.translation, orc)
        if chave not in vistos:
            vistos.add(chave)
            unicos.append(chave)

    # proteger nao depende do orcamento, entao a chave aqui e so o texto
    protegidos = {t: protect_tags(t) for t, _ in unicos}

    melhor: dict[Alvo, str] = {}
    for alvo in unicos:
        guardado = cache.get(_chave(alvo[0], alvo[1], max_line, max_lines))
        if guardado is not None:
            melhor[alvo] = guardado
            rep.do_cache += 1

    def _cabe(protegido: str, texto_base: str, orcamento: int) -> bool:
        # largura primeiro: e O(len), enquanto box_overflow quebra o texto todo
        # e, na maioria dos casos, e a largura que reprova
        if orcamento and visible_width(protegido) > orcamento:
            return False
        # ⚠ cru vs protegido importa nos DOIS sentidos: visible_width tem que ver
        # o protegido (senao o marcador reprova a fala), e box_overflow tem que
        # ver o restaurado (no protegido a quebra literal virou placeholder e
        # line_count nao a veria)
        restaurado, _ = restore_tags(protegido, protegidos[texto_base][1])
        return box_overflow(restaurado, max_line, max_lines, nl) == 0

    def _rodada(pendentes: list[Alvo], resumir: bool) -> None:
        rotulo = "resumo" if resumir else "reescrita"
        for ini in range(0, len(pendentes), batch_size):
            lote = pendentes[ini:ini + batch_size]
            payload = [protegidos[t][0] for t, _ in lote]
            orcamentos = [orc for _, orc in lote]
            saida = _com_retry(chamar_modelo, payload, orcamentos,
                               resumir, retries, delay, log)
            if len(saida) != len(payload):
                raise TranslationError(
                    f"o modelo devolveu {len(saida)} falas para {len(payload)} pedidas")
            for alvo, encurtado in zip(lote, saida):
                limpo = " ".join(encurtado.split())
                melhor[alvo] = limpo
                if cache_path is not None:
                    cache[_chave(alvo[0], alvo[1], max_line, max_lines)] = limpo
            if cache_path is not None:
                _salvar_cache(cache_path, cache)
            log(f"  {rotulo}: {min(ini + batch_size, len(pendentes))}/{len(pendentes)}")

    # passada 1 - reescrever o que nao veio do cache
    pendentes = [a for a in unicos if a not in melhor]
    if pendentes:
        _rodada(pendentes, resumir=False)

    # passada 2 - resumir so o que continua estourando
    ainda: set[Alvo] = {a for a in unicos
                        if not _cabe(melhor.get(a, protegidos[a[0]][0]), a[0], a[1])}
    if ainda:
        log(f"  {len(ainda)} falas nao couberam na reescrita; tentando resumir")
        _rodada([a for a in unicos if a in ainda], resumir=True)

    # aplica nas entradas
    for e, orc in alvos:
        base = e.translation
        alvo = (base, orc)
        protegido = melhor.get(alvo)
        if protegido is None:
            continue
        restaurado, ok = restore_tags(protegido, protegidos[base][1])
        novo = wrap_text(restaurado, max_line, nl)
        linhas_antes = line_count(wrap_text(base, max_line, nl))
        linhas_depois = line_count(novo)
        col_antes, col_depois = display_width(base), display_width(novo)
        sobra_linhas = box_overflow(novo, max_line, max_lines, nl)
        falta_largura = max(0, col_depois - orc) if orc else 0

        # so descarta quando piorou nos DOIS criterios. A guarda antiga olhava so
        # linha, e por isso jogava fora uma reescrita que cortava 25 colunas mas
        # continuava com o mesmo numero de linhas - exatamente o caso util aqui.
        piorou = linhas_depois >= linhas_antes and col_depois >= col_antes
        if piorou and (sobra_linhas or falta_largura):
            e.needs_review = True
            e.notes.append(
                f"nao coube ({col_depois} colunas para um orcamento de {orc}) e o "
                f"encurtamento nao reduziu; faltam {falta_largura} - encurte a mao")
            rep.restantes += 1
            continue

        e.translation = novo
        if not ok:
            e.needs_review = True
            e.notes.append("marcadores perdidos no encurtamento - revisar manualmente")
            rep.marcadores_perdidos += 1
        if sobra_linhas or falta_largura:
            e.needs_review = True
            partes = []
            if falta_largura:
                partes.append(f"faltam {falta_largura} colunas (orcamento {orc})")
            if sobra_linhas:
                partes.append(f"passa {sobra_linhas} linha(s) da caixa")
            e.notes.append(" e ".join(partes) + " - encurte a mao")
            rep.restantes += 1
            # o texto original so vai para a nota quando alguem precisa revisar:
            # anexar em todo sucesso incharia o .json a toa
            e.notes.append(f"antes: {base!r}")
        elif alvo in ainda:
            rep.resolvidas_2 += 1
        else:
            rep.resolvidas_1 += 1
        if len(rep.exemplos) < 50:
            rep.exemplos.append(Exemplo(e.id, base, novo, col_antes, col_depois,
                                        orc, linhas_antes, linhas_depois))

    if cache_path is not None:
        _salvar_cache(cache_path, cache)
    return rep


def linhas_por_fala(entries: Iterable[TextEntry], max_line: int = MAX_LINE_DEFAULT,
                    newline: str = "auto") -> list[int]:
    """
    Quantas linhas cada fala traduzida ocupa. Serve para o histograma do --dry-run.

    Um "0 estouram" sozinho e ambiguo: pode ser que nada esteja errado, ou que
    nao houvesse o que medir. A distribuicao desfaz a duvida - e um monte de
    falas batendo EXATAMENTE no limite denuncia que a largura assumida esta
    maior que a caixa de verdade.
    """
    itens = list(entries)
    nl = detect_newline(itens, None if newline == "auto" else newline)
    return [line_count(wrap_text(e.translation, max_line, nl))
            for e in itens if e.translation.strip()
            and classify_text(e.original) != "id"]


def relatorio_seco(entries: Iterable[TextEntry], max_line: int = MAX_LINE_DEFAULT,
                   max_lines: int = MAX_LINES_DEFAULT,
                   newline: str = "auto", folga: float = FOLGA_DEFAULT,
                   percentil: int = PISO_PERCENTIL, tolerancia: int = 0,
                   usar_original: bool = True, piso_fixo: int = 0
                   ) -> list[tuple[TextEntry, int, int, int]]:
    """
    O que estoura, sem chamar a API (--dry-run).

    Devolve (entrada, linhas, colunas, orcamento), ordenado pelo DEFICIT
    decrescente: quem precisa cortar mais aparece primeiro, que e a ordem util
    para decidir se vale gastar.
    """
    itens = list(entries)
    nl = detect_newline(itens, None if newline == "auto" else newline)
    out = []
    for e, orc in candidatas_orcadas(itens, max_line, max_lines, newline,
                                     folga, percentil, tolerancia, usar_original,
                                     piso_fixo):
        quebrada = wrap_text(e.translation, max_line, nl)
        out.append((e, line_count(quebrada), display_width(e.translation), orc))
    out.sort(key=lambda t: (t[2] - t[3]) if t[3] else 0, reverse=True)
    return out


def larguras_dos_originais(entries: Iterable[TextEntry]) -> list[int]:
    """
    As larguras das falas ORIGINAIS. E a distribuicao que define a caixa.

    O percentil usado como piso e um chute enquanto ninguem olha esta curva: se
    a massa esta em 60 colunas e o P90 devolve 39, a populacao tem coisa que nao
    e fala exibida - ou o percentil esta baixo demais para o que a premissa diz
    ("o jogo mostrou isto, logo cabe").
    """
    return sorted(display_width(t) for t in originais_de_fala(entries))


def geometria_dos_originais(entries: Iterable[TextEntry]) -> tuple[list[int], dict[int, int]]:
    """
    A largura de cada LINHA das falas originais, e quantas linhas cada fala tem.

    `display_width` soma o texto todo, o que confunde duas coisas bem diferentes:
    uma fala de 80 colunas numa linha so prova uma caixa larga; a mesma fala
    quebrada em tres prova uma caixa estreita e alta. Como o script traz as
    quebras que o jogo desenhou, a geometria da caixa esta nos dados - e e a
    medicao que a regua nao conseguiu arrancar do jogo.
    """
    larguras: list[int] = []
    contagem: dict[int, int] = {}
    for texto in originais_de_fala(entries):
        linhas = _SPLIT_NEWLINE_RE.split(texto)[::2]
        larguras.extend(display_width(l) for l in linhas if l.strip())
        n = len([l for l in linhas if l.strip()])
        contagem[n] = contagem.get(n, 0) + 1
    return sorted(larguras), contagem


def perfil_dos_originais(entries: Iterable[TextEntry]) -> dict[str, int]:
    """
    Que tipo de texto esta no campo `original`.

    Num roteiro japones a esmagadora maioria tem que ser "cjk". Muita "prose"
    ali e a assinatura de um .json contaminado - extraido de uma saida ja
    injetada, ou lido na codificacao errada. Medir a caixa por originais assim
    mede a coisa errada, e encurtar por eles gasta dinheiro a toa.
    """
    perfil = {"cjk": 0, "prose": 0, "id": 0}
    for e in entries:
        if getattr(e, "original", "").strip():
            perfil[classify_text(e.original)] += 1
    return perfil


def amostra_de_prosa(entries: Iterable[TextEntry], quantas: int = 6) -> list[str]:
    """
    Alguns originais classificados como prosa latina.

    O numero sozinho nao distingue as duas causas possiveis: `.json`
    contaminado (o `original` virou portugues) ou texto latino que sempre
    esteve no script (nome de cenario, palavra da engine). Ler tres exemplos
    resolve em um segundo o que a estatistica deixa ambiguo.
    """
    out: list[str] = []
    for e in entries:
        texto = getattr(e, "original", "")
        if texto.strip() and classify_text(texto) == "prose":
            out.append(texto)
            if len(out) >= quantas:
                break
    return out


def percentis(valores: list[int]) -> dict[str, int]:
    """P50/P75/P90/P95/P99 e o maximo, para escolher o piso com dado e nao chute."""
    if not valores:
        return {}
    def _p(q: int) -> int:
        i = max(0, math.ceil(len(valores) * q / 100) - 1)
        return valores[min(i, len(valores) - 1)]
    return {"P50": _p(50), "P75": _p(75), "P90": _p(90),
            "P95": _p(95), "P99": _p(99), "max": valores[-1]}


def piso_e_teto(entries: Iterable[TextEntry], max_line: int = MAX_LINE_DEFAULT,
                max_lines: int = MAX_LINES_DEFAULT,
                percentil: int = PISO_PERCENTIL) -> tuple[int, int]:
    """O piso calculado neste lote e o teto da caixa - para o --dry-run exibir."""
    return (piso_do_lote(originais_de_fala(entries), percentil),
            box_budget(max_line, max_lines))
