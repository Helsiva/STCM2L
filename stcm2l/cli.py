"""
stcm2l.cli
==========

Interface de linha de comando. Todos os subcomandos aceitam arquivo unico ou
pasta (processamento em lote).

    python stcm2l.py info      <arquivo|pasta>
    python stcm2l.py verify    <arquivo|pasta>
    python stcm2l.py extract   <arquivo|pasta> -o <saida>
    python stcm2l.py translate <json|pasta>    -o <saida> --provider deepl --api-key KEY
    python stcm2l.py inject    <arquivo|pasta> --texts <json|pasta> -o <saida>
    python stcm2l.py selftest
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from . import __version__
from .core import Stcm2lError
from .pipeline import (
    DAT_SUFFIXES, extract_file, inject_file, inspect, iter_inputs, verify_file,
)
from .textio import dump_entries, load_entries
from .translate import TranslationError, translate_entries

TEXT_SUFFIXES = (".json", ".txt")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _looks_like_dir(path: Path, forced: bool) -> bool:
    return forced or path.is_dir() or path.suffix == ""


def _out_for(src: Path, out: Path, suffix: str, batch: bool) -> Path:
    """Resolve o caminho de saida para um arquivo de entrada."""
    if batch or _looks_like_dir(out, False):
        return out / (src.stem + suffix)
    return out


def _pair_texts(dat: Path, texts_root: Path) -> Path | None:
    """Encontra o .json/.txt correspondente a um .DAT."""
    if texts_root.is_file():
        return texts_root
    for suffix in TEXT_SUFFIXES:
        cand = texts_root / (dat.stem + suffix)
        if cand.exists():
            return cand
    return None


# ---------------------------------------------------------------------------
# Subcomandos
# ---------------------------------------------------------------------------

def cmd_info(args: argparse.Namespace) -> int:
    files = iter_inputs(Path(args.input), args.recursive, args.suffixes)
    if not files:
        print("nenhum arquivo encontrado.")
        return 1
    for path in files:
        try:
            nfo = inspect(path, args.encoding)
        except Stcm2lError as exc:
            print(f"{path.name}: ERRO - {exc}")
            continue
        print(f"\n== {path} ==")
        print(f"  tamanho .......... {nfo.size} bytes")
        print(f"  magic ............ {nfo.magic!r}")
        print(f"  export_offset .... 0x{nfo.export_offset:08X}  ({nfo.export_count} exports)")
        print(f"  collection_link .. 0x{nfo.collection_link:08X}")
        print(f"  acoes ............ {nfo.actions}   chunks crus: {nfo.raw_chunks}")
        print(f"  blocos de dado ... {nfo.data_blocks}   com texto: {nfo.text_blocks}")
        print(f"  encoding ......... {nfo.encoding}")
        if nfo.opcodes:
            top = "  ".join(f"0x{op:X}x{n}" for op, n in nfo.opcodes)
            print(f"  opcodes (top) .... {top}")
        for w in nfo.warnings:
            print(f"  ! aviso: {w}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    files = iter_inputs(Path(args.input), args.recursive, args.suffixes)
    if not files:
        print("nenhum arquivo encontrado.")
        return 1
    bad = 0
    for path in files:
        try:
            same, detail = verify_file(path, relocate=args.relocate)
        except Stcm2lError as exc:
            print(f"[ERRO ] {path.name}: {exc}")
            bad += 1
            continue
        print(f"[{'  OK  ' if same else 'DIFERE'}] {path.name}: {detail}")
        bad += 0 if same else 1
    total = len(files)
    print(f"\n{total - bad}/{total} arquivos reconstroem identicos ao original.")
    if bad:
        print("Arquivos que diferem NAO devem ser injetados: abra um issue com o .DAT "
              "ou use --relocate strict para comparar.")
    return 1 if bad else 0


def cmd_extract(args: argparse.Namespace) -> int:
    src = Path(args.input)
    out = Path(args.output)
    files = iter_inputs(src, args.recursive, args.suffixes)
    if not files:
        print("nenhum arquivo encontrado.")
        return 1
    batch = src.is_dir()
    if batch:
        out.mkdir(parents=True, exist_ok=True)
    suffix = ".json" if args.format == "json" else ".txt"
    total = 0
    fails = 0
    for path in files:
        target = _out_for(path, out, suffix, batch)
        try:
            n = extract_file(path, target, fmt=args.format,
                             forced_encoding=args.encoding, all_blocks=args.all_blocks)
        except Stcm2lError as exc:
            print(f"[ERRO] {path.name}: {exc}")
            fails += 1
            continue
        total += n
        print(f"[ OK ] {path.name}: {n} textos -> {target}")
    print(f"\n{total} textos extraidos de {len(files) - fails}/{len(files)} arquivos.")
    return 1 if fails else 0


def cmd_translate(args: argparse.Namespace) -> int:
    src = Path(args.input)
    out = Path(args.output)
    files = iter_inputs(src, args.recursive, TEXT_SUFFIXES)
    if not files:
        print("nenhum arquivo de texto (.json/.txt) encontrado.")
        return 1
    batch = src.is_dir()
    if batch:
        out.mkdir(parents=True, exist_ok=True)
    cache = Path(args.cache) if args.cache else None
    done = 0
    for path in files:
        print(f"\n-> {path.name}")
        entries, meta = load_entries(path)
        try:
            n = translate_entries(
                entries, provider=args.provider, api_key=args.api_key,
                source=args.source, target=args.target, batch_size=args.batch_size,
                retries=args.retries, delay=args.delay, cache_path=cache,
                overwrite=args.overwrite,
            )
        except TranslationError as exc:
            print(f"  ERRO de traducao: {exc}")
            return 2
        done += n
        target = _out_for(path, out, path.suffix, batch)
        fmt = "json" if target.suffix.lower() == ".json" else "txt"
        dump_entries(entries, target, meta.get("source", path.stem),
                     meta.get("encoding", "utf-8"), fmt)
        review = sum(1 for e in entries if e.needs_review)
        print(f"  {n} traduzidos -> {target}" + (f"  ({review} para revisar)" if review else ""))
    print(f"\nTotal traduzido: {done}")
    return 0


def cmd_inject(args: argparse.Namespace) -> int:
    src = Path(args.input)
    texts = Path(args.texts)
    out = Path(args.output)
    files = iter_inputs(src, args.recursive, args.suffixes)
    if not files:
        print("nenhum arquivo encontrado.")
        return 1
    batch = src.is_dir()
    if batch:
        out.mkdir(parents=True, exist_ok=True)
    fails = 0
    applied = 0
    for path in files:
        pair = _pair_texts(path, texts)
        if pair is None:
            print(f"[PULA] {path.name}: sem .json/.txt correspondente em {texts}")
            continue
        target = _out_for(path, out, path.suffix, batch)
        if target.resolve() == path.resolve():
            print(f"[ERRO] {path.name}: a saida sobrescreveria o original. Use outra pasta.")
            fails += 1
            continue
        try:
            rep = inject_file(
                path, pair, target, out_encoding=args.out_encoding,
                fallback=args.fallback, relocate=args.relocate,
                fix_len_params=not args.no_fix_len, strict_match=args.strict,
            )
        except (Stcm2lError, UnicodeEncodeError) as exc:
            print(f"[ERRO] {path.name}: {exc}")
            if isinstance(exc, UnicodeEncodeError):
                print("       dica: o texto PT-BR nao cabe na codificacao do jogo. "
                      "Use --out-encoding utf-8 ou --fallback ascii.")
            fails += 1
            continue
        applied += rep.applied
        print(f"[ OK ] {path.name}: {rep.applied} textos, {rep.skipped} pulados, "
              f"{rep.length_params_fixed} tamanhos corrigidos -> {target}")
        for p in rep.problems:
            print(f"       ! {p}")
    print(f"\n{applied} textos injetados em {len(files) - fails}/{len(files)} arquivos.")
    return 1 if fails else 0


def cmd_selftest(args: argparse.Namespace) -> int:
    from .selftest import run
    return 0 if run() else 1


# ---------------------------------------------------------------------------
# Parser de argumentos
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="stcm2l",
        description="Kit de traducao para scripts STCM2L (Otomate/Rejet - PS Vita).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Fluxo recomendado:\n"
            "  1. python stcm2l.py verify  .\\scripts            (o parser entende seus arquivos?)\n"
            "  2. python stcm2l.py extract .\\scripts -o .\\txt\n"
            "  3. python stcm2l.py translate .\\txt -o .\\txt_ptbr --provider deepl --api-key KEY\n"
            "  4. (revisao manual dos .json)\n"
            "  5. python stcm2l.py inject  .\\scripts --texts .\\txt_ptbr -o .\\out\n"
        ),
    )
    p.add_argument("--version", action="version", version=f"stcm2l-tool {__version__}")
    p.add_argument("--traceback", action="store_true", help="mostra o stack trace completo em erros")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("-r", "--recursive", action="store_true",
                        help="varre subpastas quando a entrada e uma pasta")
        sp.add_argument("--suffixes", nargs="+", default=list(DAT_SUFFIXES),
                        help=f"extensoes tratadas como script (padrao: {' '.join(DAT_SUFFIXES)})")

    sp = sub.add_parser("info", help="mostra cabecalho, opcodes e estatisticas")
    sp.add_argument("input")
    sp.add_argument("--encoding", help="forca a codificacao de leitura (ex.: cp932)")
    common(sp)
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("verify", help="round-trip: le e reescreve, comparando byte a byte")
    sp.add_argument("input")
    sp.add_argument("--relocate", choices=("scan", "strict"), default="scan")
    common(sp)
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("extract", help="STCM2L -> .json/.txt")
    sp.add_argument("input")
    sp.add_argument("-o", "--output", required=True, help="arquivo ou pasta de saida")
    sp.add_argument("--format", choices=("json", "txt"), default="json")
    sp.add_argument("--encoding", help="forca a codificacao (padrao: deteccao automatica)")
    sp.add_argument("--all-blocks", action="store_true",
                    help="exporta TODOS os blocos de dado, inclusive os que nao parecem texto")
    common(sp)
    sp.set_defaults(func=cmd_extract)

    sp = sub.add_parser("translate", help="traduz o .json/.txt extraido para PT-BR")
    sp.add_argument("input")
    sp.add_argument("-o", "--output", required=True)
    sp.add_argument("--provider", choices=("deepl", "deepl-pro", "google", "googletrans", "none"),
                    default="deepl")
    sp.add_argument("--api-key", help="chave da API (DeepL ou Google Cloud Translation)")
    sp.add_argument("--source", help="idioma de origem (JA, EN...). Padrao: deteccao do provedor")
    sp.add_argument("--target", default="PT-BR")
    sp.add_argument("--batch-size", type=int, default=40)
    sp.add_argument("--retries", type=int, default=3)
    sp.add_argument("--delay", type=float, default=1.0, help="pausa entre lotes, em segundos")
    sp.add_argument("--cache", help="arquivo .json de cache de traducoes (reaproveita repeticoes)")
    sp.add_argument("--overwrite", action="store_true",
                    help="retraduz entradas que ja possuem traducao")
    sp.add_argument("-r", "--recursive", action="store_true")
    sp.set_defaults(func=cmd_translate)

    sp = sub.add_parser("inject", help=".json/.txt -> STCM2L (recalcula os ponteiros)")
    sp.add_argument("input", help="arquivo .DAT original ou pasta com os originais")
    sp.add_argument("--texts", required=True, help="arquivo ou pasta com as traducoes")
    sp.add_argument("-o", "--output", required=True, help="arquivo ou pasta de saida")
    sp.add_argument("--out-encoding",
                    help="codificacao de gravacao (padrao: a mesma do arquivo de traducao)")
    sp.add_argument("--fallback", choices=("strict", "ascii", "replace"), default="strict",
                    help="o que fazer com caracteres que nao cabem na codificacao do jogo")
    sp.add_argument("--relocate", choices=("scan", "strict"), default="scan")
    sp.add_argument("--no-fix-len", action="store_true",
                    help="nao atualiza parametros que repetem o tamanho da string")
    sp.add_argument("--strict", action="store_true",
                    help="aborta se o texto original do .DAT divergir do arquivo de traducao")
    common(sp)
    sp.set_defaults(func=cmd_inject)

    sp = sub.add_parser("selftest", help="roda a bateria de testes internos")
    sp.set_defaults(func=cmd_selftest)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Stcm2lError as exc:
        if args.traceback:
            traceback.print_exc()
        print(f"ERRO: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrompido pelo usuario.", file=sys.stderr)
        return 130
