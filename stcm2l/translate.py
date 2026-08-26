"""
stcm2l.translate
================

Traducao automatica JA/EN -> PT-BR com protecao dos marcadores da engine.

Provedores:
  * deepl      - API oficial (Free ou Pro). Apenas urllib (sem dependencias).
  * google     - Cloud Translation API v2 por chave. Apenas urllib.
  * googletrans- biblioteca `googletrans` (opcional, endpoint nao oficial).
  * none       - nao traduz (util para so preencher o cache/estrutura).

Todo texto passa por protect_tags() antes e restore_tags() depois: marcadores
como #Name[2], #KW_F[], {var} e \\n nunca sao enviados ao tradutor.
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Iterable

from .core import Stcm2lError
from .textio import TextEntry, classify_text, protect_tags, restore_tags

DEEPL_FREE = "https://api-free.deepl.com/v2/translate"
DEEPL_PRO = "https://api.deepl.com/v2/translate"
GOOGLE_V2 = "https://translation.googleapis.com/language/translate/v2"
GTX = "https://translate.googleapis.com/translate_a/single"


class TranslationError(Stcm2lError):
    """Falha de comunicacao com o servico de traducao."""

    #: quando True, repetir nao adianta (bloqueio, chave invalida) e a
    #: orquestracao para na hora em vez de gastar as tentativas
    fatal = False
    #: quando True, e passageiro (timeout, queda de rede, 429, 5xx): vale repetir
    transient = False


class FatalTranslationError(TranslationError):
    fatal = True


class TransientTranslationError(TranslationError):
    transient = True


def _ssl_context() -> ssl.SSLContext:
    """
    Contexto TLS preferindo o pacote `certifi`, quando instalado.

    No Windows o Python valida contra a loja de certificados do sistema, que em
    maquina desatualizada ainda carrega raizes vencidas - e ai um servidor
    perfeitamente valido volta como "certificate has expired".
    """
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _erro_de_rede(exc: Exception, url: str) -> TranslationError:
    """Traduz a excecao do urllib numa mensagem util, marcando se vale repetir."""
    alvo = url.split("?")[0]
    if isinstance(exc, urllib.error.HTTPError):
        body = exc.read().decode("utf-8", "replace")[:200]
        erro = (TransientTranslationError if exc.code in (429, 500, 502, 503, 504)
                else TranslationError)
        return erro(f"HTTP {exc.code} de {alvo}: {body}")

    # o urllib embrulha o erro de socket/TLS dentro de URLError.reason, entao a
    # causa real so aparece depois de desembrulhar
    causa = exc.reason if isinstance(exc, urllib.error.URLError) else exc

    if isinstance(causa, ssl.SSLCertVerificationError) or \
            "CERTIFICATE_VERIFY" in str(causa):
        detalhe = getattr(causa, "verify_message", None) or (
            causa.args[0] if getattr(causa, "args", None) else str(causa))
        return FatalTranslationError(
            f"o certificado TLS de {alvo} nao passou na validacao ({detalhe}). "
            "Quase sempre e a maquina, nao o servidor: rode `pip install certifi` "
            "(a ferramenta passa a usa-lo automaticamente) e confira a data e a "
            "hora do Windows, que tambem derrubam a validacao quando estao erradas."
        )
    if isinstance(causa, (TimeoutError, socket.timeout)) or "timed out" in str(causa):
        return TransientTranslationError(f"tempo esgotado esperando {alvo}")
    if isinstance(exc, urllib.error.URLError):
        return TransientTranslationError(f"falha de rede ao chamar {alvo}: {causa}")
    return TransientTranslationError(f"falha ao chamar {alvo}: {exc}")


#: erros que o urllib pode soltar SEM embrulhar em URLError - o timeout de
#: leitura e o caso classico, e ele passava direto pelo retry
_ERROS_DE_REDE = (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError,
                  http.client.HTTPException, ConnectionError, OSError)


def _post(url: str, payload: bytes, headers: dict[str, str], timeout: int = 30) -> dict:
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except _ERROS_DE_REDE as exc:
        raise _erro_de_rede(exc, url) from exc


def _get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            return resp.read().decode("utf-8")
    except _ERROS_DE_REDE as exc:
        raise _erro_de_rede(exc, url) from exc


#: variantes de `client` do endpoint publico. "gtx" e a mais conhecida e tambem a
#: mais bloqueada; "dict-chrome-ex" devolve exatamente o mesmo JSON e costuma
#: passar onde a outra leva 429. A que funcionar fica memorizada para as demais.
GTX_CLIENTS = ("dict-chrome-ex", "gtx", "at")
_gtx_client: str | None = None


def gtx_one(text: str, source: str | None, target: str) -> str:
    """
    Um texto pelo endpoint publico translate_a/single (o mesmo que o site usa).

    Nao precisa de chave nem da biblioteca googletrans - que quebra sozinha
    quando o Google mexe no formato de resposta.
    """
    global _gtx_client
    tentativas = (_gtx_client,) if _gtx_client else GTX_CLIENTS
    ultimo: TranslationError | None = None
    for client in tentativas:
        params = urllib.parse.urlencode({
            "client": client, "sl": source or "auto", "tl": target,
            "dt": "t", "q": text,
        })
        try:
            raw = _get(f"{GTX}?{params}")
        except TranslationError as exc:
            ultimo = exc
            if "HTTP 429" in str(exc) or "HTTP 403" in str(exc):
                continue          # cliente barrado: tenta a proxima variante
            raise
        _gtx_client = client
        try:
            data = json.loads(raw)
            return "".join(seg[0] for seg in data[0] if seg and seg[0])
        except (ValueError, IndexError, TypeError) as exc:
            raise TranslationError(
                f"resposta inesperada do endpoint gtx: {raw[:200]}"
            ) from exc
    raise ultimo or TranslationError("endpoint gtx indisponivel")


def gtx_batch(texts: list[str], source: str | None, target: str = "pt",
              pause: float = 0.15,
              on_done: Callable[[int, str], None] | None = None) -> list[str]:
    """
    O endpoint atende um texto por vez; a pausa evita o 429.

    `on_done(indice, traducao)` e chamado a cada texto pronto: como cada um
    custa uma requisicao, quem chama pode gravar o cache no caminho e nao perder
    o lote inteiro se o 39o texto der timeout.
    """
    out = []
    for i, text in enumerate(texts):
        for attempt in range(1, 4):
            try:
                out.append(gtx_one(text, source, target))
                break
            except TranslationError as exc:
                if not exc.transient:
                    raise
                if attempt == 3 and "HTTP 429" not in str(exc):
                    raise
                if attempt == 3:
                    raise FatalTranslationError(
                        "o endpoint gtx respondeu 429 em todas as variantes de "
                        "cliente. Ele e gratuito e nao oficial: costuma barrar por "
                        "volume e por IP. Aumente --delay, tente de novo mais tarde, "
                        "ou use --provider deepl com uma chave Free (500 mil "
                        "caracteres/mes, sem custo)."
                    ) from exc
                time.sleep(2.0 * attempt)
        if on_done:
            on_done(i, out[-1])
        if pause and i + 1 < len(texts):
            time.sleep(pause)
    return out


def deepl_batch(texts: list[str], api_key: str, source: str | None,
                target: str = "PT-BR", pro: bool = False) -> list[str]:
    url = DEEPL_PRO if pro else DEEPL_FREE
    fields: list[tuple[str, str]] = [("target_lang", target)]
    if source:
        fields.append(("source_lang", source.upper()))
    fields += [("text", t) for t in texts]
    data = urllib.parse.urlencode(fields).encode("utf-8")
    headers = {
        "Authorization": f"DeepL-Auth-Key {api_key}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    out = _post(url, data, headers)
    return [item["text"] for item in out.get("translations", [])]


def google_batch(texts: list[str], api_key: str, source: str | None,
                 target: str = "pt-BR") -> list[str]:
    payload = {"q": texts, "target": target, "format": "text"}
    if source:
        payload["source"] = source
    data = json.dumps(payload).encode("utf-8")
    url = f"{GOOGLE_V2}?key={urllib.parse.quote(api_key)}"
    out = _post(url, data, {"Content-Type": "application/json"})
    return [item["translatedText"] for item in out["data"]["translations"]]


def googletrans_batch(texts: list[str], source: str | None, target: str = "pt") -> list[str]:
    try:
        from googletrans import Translator  # type: ignore
    except ImportError as exc:
        raise TranslationError(
            "provider 'googletrans' exige `pip install googletrans==4.0.0rc1`"
        ) from exc
    tr = Translator()
    try:
        res = tr.translate(texts, src=source or "auto", dest=target)
    except (TypeError, AttributeError, IndexError) as exc:
        raise TranslationError(
            "a biblioteca googletrans quebrou ao ler a resposta do Google "
            f"({exc}). Ela depende de um formato nao oficial que muda sem aviso. "
            "Use --provider gtx, que fala com o mesmo endpoint usando so a "
            "biblioteca padrao."
        ) from exc
    if not isinstance(res, list):
        res = [res]
    return [r.text for r in res]


# ---------------------------------------------------------------------------
# Orquestracao
# ---------------------------------------------------------------------------

def translate_entries(entries: list[TextEntry], provider: str, api_key: str | None,
                      source: str | None = None, target: str = "PT-BR",
                      batch_size: int = 40, retries: int = 3, delay: float = 1.0,
                      cache_path: Path | None = None, overwrite: bool = False,
                      only_cjk: bool = False, skip_ids: bool = False,
                      log: Callable[[str], None] = print) -> int:
    """
    Traduz in-place o campo `translation` das entradas. Retorna quantas foram
    efetivamente traduzidas. Textos identicos sao traduzidos uma unica vez.
    """
    if provider == "none":
        return 0
    if provider in ("deepl", "deepl-pro", "google") and not api_key:
        raise TranslationError(f"o provedor '{provider}' exige --api-key")

    cache: dict[str, str] = {}
    if cache_path and cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        log(f"  cache carregado: {len(cache)} traducoes reaproveitaveis")

    pending = [e for e in entries if (overwrite or not e.translation.strip()) and e.original.strip()]
    if only_cjk or skip_ids:
        antes = len(pending)
        aceitos = {"cjk"} if only_cjk else {"cjk", "prose"}
        pending = [e for e in pending if classify_text(e.original) in aceitos]
        rotulo = "--only-cjk" if only_cjk else "--skip-ids"
        log(f"  {rotulo}: {antes - len(pending)} entradas preservadas sem traduzir "
            "(IDs de voz, nomes de arquivo, flags)")
    # deduplica por texto protegido
    prepared: dict[str, tuple[str, list[str]]] = {}
    for e in pending:
        if e.original not in prepared:
            prepared[e.original] = protect_tags(e.original)

    todo = [orig for orig in prepared if orig not in cache]
    log(f"  {len(pending)} entradas / {len(prepared)} textos unicos / {len(todo)} a traduzir")

    def _registrar(original: str, traduzido: str) -> None:
        """Guarda no cache assim que um texto fica pronto, gravando de vez em quando."""
        cache[original] = traduzido
        if cache_path and len(cache) % 25 == 0:
            _salvar_cache(cache_path, cache)

    for start in range(0, len(todo), batch_size):
        chunk = todo[start:start + batch_size]
        payload = [prepared[o][0] for o in chunk]
        for attempt in range(1, retries + 1):
            try:
                if provider == "deepl":
                    got = deepl_batch(payload, api_key or "", source, target, pro=False)
                elif provider == "deepl-pro":
                    got = deepl_batch(payload, api_key or "", source, target, pro=True)
                elif provider == "google":
                    got = google_batch(payload, api_key or "", source, target.lower())
                elif provider == "gtx":
                    got = gtx_batch(
                        payload, source, target.split("-")[0].lower(),
                        on_done=lambda i, txt: _registrar(chunk[i], txt),
                    )
                elif provider == "googletrans":
                    got = googletrans_batch(payload, source, target.split("-")[0].lower())
                else:
                    raise TranslationError(f"provedor desconhecido: {provider}")
                break
            except TranslationError as exc:
                if exc.fatal or attempt == retries:
                    _salvar_cache(cache_path, cache, log)
                    raise
                log(f"  tentativa {attempt}/{retries} falhou ({exc}); repetindo em {delay * attempt:.0f}s")
                time.sleep(delay * attempt)
        if len(got) != len(payload):
            raise TranslationError(
                f"o provedor devolveu {len(got)} traducoes para {len(payload)} textos"
            )
        for orig, translated in zip(chunk, got):
            cache[orig] = translated
        # grava a cada lote: se a proxima chamada morrer, o que ja foi traduzido
        # (e pago) nao se perde - basta rodar de novo com o mesmo --cache
        _salvar_cache(cache_path, cache)
        log(f"  traduzidos {min(start + batch_size, len(todo))}/{len(todo)}")
        if delay:
            time.sleep(delay)

    done = 0
    for e in pending:
        protected_translation = cache.get(e.original)
        if protected_translation is None:
            continue
        _, tags = prepared[e.original]
        final, ok = restore_tags(protected_translation, tags)
        e.translation = final
        e.needs_review = not ok
        if not ok:
            e.notes.append("marcadores perdidos pelo tradutor - revisar manualmente")
        done += 1

    _salvar_cache(cache_path, cache)
    return done


def _salvar_cache(cache_path: Path | None, cache: dict[str, str],
                  log: Callable[[str], None] | None = None) -> None:
    if not cache_path or not cache:
        return
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    if log:
        log(f"  cache gravado com {len(cache)} traducoes - rodar de novo com o mesmo "
            "--cache retoma de onde parou")
