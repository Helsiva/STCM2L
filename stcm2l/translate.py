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

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Iterable

from .core import Stcm2lError
from .textio import TextEntry, protect_tags, restore_tags

DEEPL_FREE = "https://api-free.deepl.com/v2/translate"
DEEPL_PRO = "https://api.deepl.com/v2/translate"
GOOGLE_V2 = "https://translation.googleapis.com/language/translate/v2"
GTX = "https://translate.googleapis.com/translate_a/single"


#: kana + kanji + katakana de meia largura
CJK_RE = re.compile(r"[\u3040-\u30FF\u3400-\u4DBF\u4E00-\u9FFF\uFF66-\uFF9D]")


class TranslationError(Stcm2lError):
    """Falha de comunicacao com o servico de traducao."""


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

def _post(url: str, payload: bytes, headers: dict[str, str], timeout: int = 30) -> dict:
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        raise TranslationError(f"HTTP {exc.code} de {url}: {body}") from exc
    except urllib.error.URLError as exc:
        raise TranslationError(f"falha de rede ao chamar {url}: {exc.reason}") from exc


def _get(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:200]
        raise TranslationError(f"HTTP {exc.code} de {url.split('?')[0]}: {body}") from exc
    except urllib.error.URLError as exc:
        raise TranslationError(f"falha de rede: {exc.reason}") from exc


def gtx_one(text: str, source: str | None, target: str) -> str:
    """
    Um texto pelo endpoint publico translate_a/single (o mesmo que o site usa).

    Nao precisa de chave nem da biblioteca googletrans - que quebra sozinha
    quando o Google mexe no formato de resposta.
    """
    params = urllib.parse.urlencode({
        "client": "gtx", "sl": source or "auto", "tl": target, "dt": "t", "q": text,
    })
    raw = _get(f"{GTX}?{params}")
    try:
        data = json.loads(raw)
        return "".join(seg[0] for seg in data[0] if seg and seg[0])
    except (ValueError, IndexError, TypeError) as exc:
        raise TranslationError(
            f"resposta inesperada do endpoint gtx: {raw[:200]}"
        ) from exc


def gtx_batch(texts: list[str], source: str | None, target: str = "pt",
              pause: float = 0.15) -> list[str]:
    """O endpoint atende um texto por vez; a pausa evita o 429."""
    out = []
    for i, text in enumerate(texts):
        for attempt in range(1, 4):
            try:
                out.append(gtx_one(text, source, target))
                break
            except TranslationError as exc:
                if "HTTP 429" not in str(exc):
                    raise
                if attempt == 3:
                    raise TranslationError(
                        "o endpoint gtx respondeu 429 (bloqueio por volume). Ele e "
                        "gratuito e nao oficial: costuma barrar IP de VPS/VPN e cair "
                        "em lotes grandes. Aumente --delay, ou use --provider deepl "
                        "com uma chave Free (500 mil caracteres/mes, sem custo)."
                    ) from exc
                time.sleep(2.0 * attempt)
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
                      only_cjk: bool = False,
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
    if only_cjk:
        antes = len(pending)
        pending = [e for e in pending if CJK_RE.search(e.original)]
        log(f"  --only-cjk: {antes - len(pending)} entradas sem japones preservadas "
            "(IDs de voz, nomes de arquivo, flags)")
    # deduplica por texto protegido
    prepared: dict[str, tuple[str, list[str]]] = {}
    for e in pending:
        if e.original not in prepared:
            prepared[e.original] = protect_tags(e.original)

    todo = [orig for orig in prepared if orig not in cache]
    log(f"  {len(pending)} entradas / {len(prepared)} textos unicos / {len(todo)} a traduzir")

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
                    got = gtx_batch(payload, source, target.split("-")[0].lower())
                elif provider == "googletrans":
                    got = googletrans_batch(payload, source, target.split("-")[0].lower())
                else:
                    raise TranslationError(f"provedor desconhecido: {provider}")
                break
            except TranslationError as exc:
                if attempt == retries:
                    raise
                log(f"  tentativa {attempt}/{retries} falhou ({exc}); repetindo em {delay * attempt:.0f}s")
                time.sleep(delay * attempt)
        if len(got) != len(payload):
            raise TranslationError(
                f"o provedor devolveu {len(got)} traducoes para {len(payload)} textos"
            )
        for orig, translated in zip(chunk, got):
            cache[orig] = translated
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

    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    return done
