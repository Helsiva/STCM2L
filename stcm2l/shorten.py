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
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .textio import (
    MAX_LINE_DEFAULT, MAX_LINES_DEFAULT, TextEntry, box_budget, box_overflow,
    classify_text, detect_newline, line_count, protect_tags, restore_tags,
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


# ---------------------------------------------------------------------------
# Provedor: Claude
# ---------------------------------------------------------------------------

#: Instrucoes fixas. Precisam ficar byte a byte iguais entre requisicoes, senao
#: o cache de prompt e invalidado - por isso o modo (reescrever/resumir) vai na
#: mensagem do usuario, e nao aqui.
SYSTEM_PROMPT = """\
Voce encurta falas de visual novel japonesa traduzidas para portugues do Brasil, \
para que caibam na caixa de texto do jogo.

Cada fala vem com um orcamento em colunas visiveis. Devolva uma versao que caiba \
nesse orcamento.

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
class ResumoEncurtamento:
    candidatas: int = 0
    resolvidas_1: int = 0        # couberam so com a reescrita
    resolvidas_2: int = 0        # precisaram do resumo
    restantes: int = 0           # nem o resumo resolveu - marcadas para revisao
    marcadores_perdidos: int = 0
    do_cache: int = 0
    #: (id, antes, depois, linhas_antes, linhas_depois)
    exemplos: list[tuple[str, str, str, int, int]] = field(default_factory=list)

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


def _chave(texto: str, max_line: int, max_lines: int) -> str:
    """O orcamento entra na chave: a mesma fala com caixa diferente tem outra resposta."""
    return f"{max_line}x{max_lines}|{texto}"


def candidatas(entries: Iterable[TextEntry], max_line: int = MAX_LINE_DEFAULT,
               max_lines: int = MAX_LINES_DEFAULT,
               newline: str = "auto") -> list[TextEntry]:
    """As entradas traduzidas que estouram a caixa. Identificador nunca entra."""
    itens = list(entries)
    nl = detect_newline(itens, None if newline == "auto" else newline)
    return [e for e in itens
            if e.translation.strip()
            and classify_text(e.original) != "id"
            and box_overflow(e.translation, max_line, max_lines, nl) > 0]


def shorten_entries(entries: list[TextEntry], chamar_modelo: Chamador, *,
                    max_line: int = MAX_LINE_DEFAULT,
                    max_lines: int = MAX_LINES_DEFAULT,
                    newline: str = "auto", batch_size: int = 25,
                    retries: int = 3, delay: float = 2.0,
                    cache_path: Path | None = None,
                    log: Callable[[str], None] = print) -> ResumoEncurtamento:
    """
    Encurta in-place o campo `translation` do que nao cabe na caixa.

    `chamar_modelo` e injetado para que o fluxo inteiro seja testavel sem rede -
    a mesma razao pela qual `_gtx_normaliza` foi extraida em translate.py.
    """
    rep = ResumoEncurtamento()
    nl = detect_newline(entries, None if newline == "auto" else newline)
    alvos = candidatas(entries, max_line, max_lines, newline)
    rep.candidatas = len(alvos)
    if not alvos:
        return rep

    cache: dict[str, str] = {}
    if cache_path and cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))

    # deduplica por texto: a mesma fala repetida no roteiro so e encurtada uma vez
    unicos: list[str] = []
    vistos: set[str] = set()
    for e in alvos:
        if e.translation not in vistos:
            vistos.add(e.translation)
            unicos.append(e.translation)

    protegidos = {t: protect_tags(t) for t in unicos}
    orcamento = box_budget(max_line, max_lines)

    #: texto original -> melhor versao PROTEGIDA obtida ate agora
    melhor: dict[str, str] = {}
    for t in unicos:
        guardado = cache.get(_chave(t, max_line, max_lines))
        if guardado is not None:
            melhor[t] = guardado
            rep.do_cache += 1

    def _cabe(protegido: str, texto_base: str) -> bool:
        restaurado, _ = restore_tags(protegido, protegidos[texto_base][1])
        return box_overflow(restaurado, max_line, max_lines, nl) == 0

    def _rodada(pendentes: list[str], resumir: bool) -> None:
        rotulo = "resumo" if resumir else "reescrita"
        for ini in range(0, len(pendentes), batch_size):
            lote = pendentes[ini:ini + batch_size]
            payload = [protegidos[t][0] for t in lote]
            saida = _com_retry(chamar_modelo, payload, [orcamento] * len(lote),
                               resumir, retries, delay, log)
            if len(saida) != len(payload):
                raise TranslationError(
                    f"o modelo devolveu {len(saida)} falas para {len(payload)} pedidas")
            for texto, encurtado in zip(lote, saida):
                limpo = " ".join(encurtado.split())
                melhor[texto] = limpo
                if cache_path is not None:
                    cache[_chave(texto, max_line, max_lines)] = limpo
            if cache_path is not None:
                _salvar_cache(cache_path, cache)
            log(f"  {rotulo}: {min(ini + batch_size, len(pendentes))}/{len(pendentes)}")

    # passada 1 - reescrever o que nao veio do cache
    pendentes = [t for t in unicos if t not in melhor]
    if pendentes:
        _rodada(pendentes, resumir=False)

    # passada 2 - resumir so o que continua estourando
    ainda = [t for t in unicos if not _cabe(melhor.get(t, protegidos[t][0]), t)]
    if ainda:
        log(f"  {len(ainda)} falas nao couberam na reescrita; tentando resumir")
        _rodada(ainda, resumir=True)

    # aplica nas entradas
    for e in alvos:
        base = e.translation
        protegido = melhor.get(base)
        if protegido is None:
            continue
        restaurado, ok = restore_tags(protegido, protegidos[base][1])
        novo = wrap_text(restaurado, max_line, nl)
        antes, depois = line_count(wrap_text(base, max_line, nl)), line_count(novo)
        if depois >= antes and box_overflow(novo, max_line, max_lines, nl) > 0:
            # o modelo nao ajudou: manter o que ja estava e sinalizar
            e.needs_review = True
            e.notes.append(
                f"nao coube na caixa ({antes} linhas, cabem {max_lines}) e o "
                f"encurtamento nao reduziu - encurte a mao")
            rep.restantes += 1
            continue
        e.translation = novo
        if not ok:
            e.needs_review = True
            e.notes.append("marcadores perdidos no encurtamento - revisar manualmente")
            rep.marcadores_perdidos += 1
        sobra = box_overflow(novo, max_line, max_lines, nl)
        if sobra:
            e.needs_review = True
            e.notes.append(
                f"ainda passa {sobra} linha(s) da caixa - encurte a mao")
            rep.restantes += 1
        else:
            if base in ainda:
                rep.resolvidas_2 += 1
            else:
                rep.resolvidas_1 += 1
        e.notes.append(f"encurtado de {antes} para {depois} linhas; antes: {base!r}")
        if len(rep.exemplos) < 50:
            rep.exemplos.append((e.id, base, novo, antes, depois))

    if cache_path is not None:
        _salvar_cache(cache_path, cache)
    return rep


def relatorio_seco(entries: Iterable[TextEntry], max_line: int = MAX_LINE_DEFAULT,
                   max_lines: int = MAX_LINES_DEFAULT,
                   newline: str = "auto") -> list[tuple[TextEntry, int, int]]:
    """
    O que estoura, sem chamar a API (--dry-run).

    Devolve (entrada, linhas_atuais, colunas_visiveis) para dimensionar o gasto
    antes de gastar.
    """
    itens = list(entries)
    nl = detect_newline(itens, None if newline == "auto" else newline)
    out = []
    for e in candidatas(itens, max_line, max_lines, newline):
        quebrada = wrap_text(e.translation, max_line, nl)
        out.append((e, line_count(quebrada), visible_width(protect_tags(e.translation)[0])))
    return out
