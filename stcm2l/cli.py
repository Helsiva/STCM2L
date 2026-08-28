"""
stcm2l.cli
==========

Interface de linha de comando. Todos os subcomandos aceitam arquivo unico ou
pasta (processamento em lote).

    python stcm2l.py info      <arquivo|pasta>
    python stcm2l.py diag      <arquivo>            (por que deu 0 textos?)
    python stcm2l.py verify    <arquivo|pasta>
    python stcm2l.py extract   <arquivo|pasta> -o <saida>
    python stcm2l.py translate <json|pasta>    -o <saida> --source JA   (gratis, sem chave)
    python stcm2l.py inject    <arquivo|pasta> --texts <json|pasta> -o <saida>
    python stcm2l.py compare   <original> --patched <injetado>   (o que mudou alem do texto?)
    python stcm2l.py selftest
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from . import __version__
from .compare import compare
from .core import Stcm2lError
from .pipeline import (
    DAT_SUFFIXES, extract_file, inject_file, inspect, iter_inputs, verify_file,
)
from .textio import MAX_LINE_DEFAULT, dump_entries, load_entries
from .translate import TranslationError, translate_entries

TEXT_SUFFIXES = (".json", ".txt")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _looks_like_dir(path: Path, forced: bool) -> bool:
    return forced or path.is_dir() or path.suffix == ""


def _out_for(src: Path, out: Path, suffix: str, batch: bool,
             root: Path | None = None) -> Path:
    """
    Resolve o caminho de saida para um arquivo de entrada.

    Com `root` (a pasta que o usuario passou), a saida ESPELHA a estrutura de
    subpastas da entrada. Sem isso o -r achatava tudo numa pasta so, e devolver
    os arquivos para dentro do container virava trabalho manual - o caminho mais
    curto para gravar um arquivo por cima do lugar errado.
    """
    if batch or _looks_like_dir(out, False):
        if root is not None:
            try:
                rel = src.relative_to(root).parent
            except ValueError:
                rel = Path()
            return out / rel / (src.stem + suffix)
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
    # arvore espelhada: o texto pode estar numa subpasta, como o .DAT esta
    for suffix in TEXT_SUFFIXES:
        achado = next(texts_root.rglob(dat.stem + suffix), None)
        if achado is not None:
            return achado
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
        k = nfo.kinds
        print(f"  conteudo ......... japones {k['cjk']} | prosa latina {k['prose']} | "
              f"identificadores {k['id']}")
        for local, rotulo in (("acao", "dentro de acoes"), ("cru", "em trechos crus")):
            w = nfo.where[local]
            if sum(w.values()):
                print(f"    {rotulo:<18} japones {w['cjk']} | prosa {w['prose']} | "
                      f"ids {w['id']}")
        print(f"  encoding ......... {nfo.encoding}")
        if nfo.encoding_scores and nfo.encoding_total:
            placar = "  ".join(f"{e}:{n}/{nfo.encoding_total}"
                               for e, n in nfo.encoding_scores if n)
            print(f"    cobertura ...... {placar}")
            if nfo.encoding in ("cp1252", "latin-1"):
                print(f"    ! {nfo.encoding} mapeia byte a byte e NUNCA falha - confira a "
                      f"amostra abaixo antes de aceitar")
        for i, exemplo in enumerate(nfo.encoding_samples or []):
            if i == 0:
                print("    mesma frase em cada codificacao:")
            distintos, vistos = [], set()
            for enc, txt in exemplo.items():
                if txt in vistos:
                    continue
                vistos.add(txt)
                distintos.append((enc, txt))
            for enc, txt in distintos:
                marca = "->" if enc == nfo.encoding else "  "
                corte = txt if len(txt) <= 58 else txt[:58] + "..."
                print(f"      {marca} {enc:<9} {corte!r}")
            if i < len(nfo.encoding_samples) - 1:
                print()
        if nfo.opcodes:
            top = "  ".join(f"0x{op:X}x{n}" for op, n in nfo.opcodes)
            print(f"  opcodes (top) .... {top}")
        for w in nfo.warnings:
            print(f"  ! aviso: {w}")
    return 0


def cmd_diag(args: argparse.Namespace) -> int:
    from .diag import diagnose
    files = iter_inputs(Path(args.input), args.recursive, args.suffixes)
    if not files:
        print("nenhum arquivo encontrado.")
        return 1
    for path in files[:args.files]:
        try:
            diagnose(path, args.encoding, limit=args.limit, min_chars=args.min_chars)
        except Stcm2lError as exc:
            print(f"{path.name}: ERRO - {exc}")
    if len(files) > args.files:
        print(f"\n({len(files) - args.files} arquivos omitidos; use --files N)")
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
        target = _out_for(path, out, suffix, batch, src if batch else None)
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
                overwrite=args.overwrite, only_cjk=args.only_cjk,
                skip_ids=args.skip_ids,
                max_line=args.max_line, newline=args.newline,
            )
        except TranslationError as exc:
            print(f"  ERRO de traducao: {exc}")
            return 2
        done += n
        target = _out_for(path, out, path.suffix, batch, src if batch else None)
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
    overflow = 0
    id_changes = 0
    divergentes = 0
    for path in files:
        pair = _pair_texts(path, texts)
        if pair is None:
            print(f"[PULA] {path.name}: sem .json/.txt correspondente em {texts}")
            continue
        target = _out_for(path, out, path.suffix, batch, src if batch else None)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.resolve() == path.resolve():
            print(f"[ERRO] {path.name}: a saida sobrescreveria o original. Use outra pasta.")
            fails += 1
            continue
        try:
            rep = inject_file(
                path, pair, target, out_encoding=args.out_encoding,
                fallback=args.fallback, relocate=args.relocate,
                fix_len_params=args.fix_len and not args.no_fix_len,
                strict_match=args.strict, ignore_mismatch=args.ignore_mismatch,
                max_line=args.max_line, newline=args.newline,
                fit=args.fit, allow_id_change=args.allow_id_change,
            )
        except (Stcm2lError, UnicodeEncodeError) as exc:
            print(f"[ERRO] {path.name}: {exc}")
            if isinstance(exc, UnicodeEncodeError):
                print("       dica: o texto PT-BR nao cabe na codificacao do jogo. "
                      "Use --out-encoding utf-8 ou --fallback ascii.")
            fails += 1
            continue
        applied += rep.applied
        overflow += rep.overflow
        divergentes += rep.divergentes
        id_changes += rep.id_changes
        extra = ""
        if rep.overflow:
            extra += f", {rep.overflow} NAO couberam"
        if rep.id_changes:
            extra += f", {rep.id_changes} identificadores"
        if args.fit:
            extra += "  [layout preservado]" if rep.layout_preserved else "  [LAYOUT MUDOU!]"
        print(f"[ OK ] {path.name}: {rep.applied} textos, {rep.skipped} pulados{extra} -> {target}")
        motivos = [
            (rep.skip_sem_traducao, "sem traducao no arquivo de texto (campo 'translation' vazio)"),
            (rep.skip_igual, "traducao identica ao original"),
            (rep.skip_id, "identificador (traducao recusada)"),
            (rep.overflow, "nao coube no bloco original (--fit)"),
            (rep.skip_divergente, "texto original nao bate com o bloco do .DAT"),
            (rep.skip_alvo, "alvo invalido (elemento/segmento nao existe no .DAT)"),
        ]
        acertos, testados = rep.match_originais
        if testados and acertos < testados:
            print(f"       o .json casa com {acertos}/{testados} textos conferidos do .DAT "
                  f"(lido como {rep.src_encoding})")
        if rep.applied == 0 and rep.skipped:
            print(f"       NADA foi injetado neste arquivo. Motivo dos {rep.skipped} pulados:")
        for n, motivo in motivos:
            if n:
                print(f"         {n:>5}  {motivo}")
        for p in rep.problems[:args.limit]:
            print(f"       ! {p}")
        if len(rep.problems) > args.limit:
            print(f"       ... e mais {len(rep.problems) - args.limit} avisos "
                  f"(--limit para ver mais)")
    print(f"\n{applied} textos injetados em {len(files) - fails}/{len(files)} arquivos.")
    if id_changes:
        print(f"{id_changes} identificador(es) tiveram traducao recusada - o jogo procura voz, "
              f"trilha e o proximo script por esse nome. Reveja o .json e apague a traducao "
              f"dessas entradas.")
    if overflow:
        print(f"{overflow} fala(s) nao couberam no bloco original e ficaram em japones. "
              f"Encurte a traducao dessas entradas e rode de novo.")
    if divergentes:
        print(f"{divergentes} entrada(s) tinham 'original' diferente do bloco do .DAT e nao "
              f"foram injetadas. Isso quer dizer que o .json foi extraido de OUTROS arquivos "
              f"(ou de uma saida ja injetada): re-extraia dos .DAT limpos e refaca a traducao.")
    if applied == 0:
        print("NENHUM texto foi injetado: a saida e uma copia do original. Se todos os "
              "pulados foram 'sem traducao', o --texts esta apontando para os .json "
              "EXTRAIDOS em vez dos TRADUZIDOS (a saida do 'translate').")
    return 1 if fails else 0


def _pair_dat(dat: Path, root: Path) -> Path | None:
    """
    Encontra o .DAT injetado que corresponde a `dat`.

    Tolera o que muda no caminho de volta do jogo: caixa da extensao (.DAT/.dat),
    subpasta espelhada e nome com sufixo (`NO00_ptbr.DAT`). Casa pelo NOME BASE,
    nunca por posicao na lista - comparar arquivo trocado daria diff falso.
    """
    if root.is_file():
        return root
    if not root.is_dir():
        return None
    alvo = root / dat.name
    if alvo.exists():
        return alvo
    stem = dat.stem.lower()
    exatos, prefixos = [], []
    for cand in root.rglob("*"):
        if not cand.is_file():
            continue
        cs = cand.stem.lower()
        if cs == stem:
            exatos.append(cand)
        elif cs.startswith(stem):
            prefixos.append(cand)
    for lista in (exatos, prefixos):
        if len(lista) == 1:
            return lista[0]
        if len(lista) > 1:
            return sorted(lista)[0]
    return None


def cmd_compare(args: argparse.Namespace) -> int:
    src = Path(args.input)
    patched = Path(args.patched)
    files = iter_inputs(src, args.recursive, args.suffixes)
    if not files:
        print("nenhum arquivo encontrado.")
        return 1
    sujos = 0
    sem_par = 0
    com_estrutura = com_ids = com_suspeitos = com_isolados = 0
    agregado: dict[tuple[int, int, int], tuple[int, int]] = {}
    for path in files:
        alvo = _pair_dat(path, patched)
        if alvo is None:
            print(f"[PULA] {path.name}: sem par em {patched}")
            sem_par += 1
            continue
        try:
            rep = compare(path, alvo)
        except Stcm2lError as exc:
            print(f"[ERRO] {path.name}: {exc}")
            sujos += 1
            continue

        delta = rep.size_b - rep.size_a
        marca = " OK " if rep.clean else "!!!!"
        print(f"\n[{marca}] {path.name}")
        print(f"       tamanho    {rep.size_a} -> {rep.size_b} ({delta:+d} bytes)"
              + ("   [layout preservado]" if delta == 0 else ""))
        print(f"       elementos  {rep.elems_a} -> {rep.elems_b}")
        tipos = {}
        for t in rep.texts:
            tipos[t.kind] = tipos.get(t.kind, 0) + 1
        resumo = ", ".join(f"{v} {k}" for k, v in sorted(tipos.items())) or "nenhum"
        print(f"       textos     {len(rep.texts)} alterados ({resumo})")
        print(f"       words      {len(rep.words)} reescritos "
              f"({len(rep.relocations)} relocacao esperada, {len(rep.suspects)} SUSPEITOS)")

        for msg in rep.structural[:args.limit]:
            print(f"       ! ESTRUTURA: {msg}")
        ids = rep.id_changes
        if ids:
            print(f"       ! {len(ids)} IDENTIFICADOR(ES) ALTERADO(S) - o jogo procura voz, "
                  f"trilha e o proximo script por esse nome:")
            for t in ids[:args.limit]:
                print(f"           [{t.elem}:{t.seg}] {t.old!r} -> {t.new!r}")
        iso = rep.isolados
        if rep.slots:
            firmes = [t for t in rep.slots if t.razao >= 0.25]
            print(f"       slots      {len(rep.slots)} (opcode, parametro, word) relocados: "
                  f"{len(firmes)} consistentes, {len(iso)} ISOLADOS")
        if iso:
            print(f"       ! {len(iso)} SLOT(S) ISOLADO(S) - relocados em pouquissimas "
                  f"instancias do opcode, cara de imediato do jogo reescrito por engano:")
            for t in iso[:args.limit]:
                print(f"           opcode 0x{t.opcode:X} param {t.pi} word {t.wi}: "
                      f"{t.relocados}/{t.instancias} instancias ({t.razao:.1%})")
        sus = rep.suspects
        if sus:
            print(f"       ! {len(sus)} WORD(S) SUSPEITO(S) - reescritos sem alvo logico "
                  f"correspondente (candidatos a quebrar o roteiro):")
            for w in sus[:args.limit]:
                op = f" op 0x{w.opcode:X}" if w.opcode is not None else ""
                print(f"           0x{w.off:06X} elem {w.elem}{op} {w.where}: "
                      f"0x{w.old:08X} -> 0x{w.new:08X}")
        if args.show_text:
            for t in rep.texts[:args.limit]:
                print(f"           [{t.elem}:{t.seg}] ({t.kind}) {t.old!r} -> {t.new!r}")
        if not rep.clean:
            sujos += 1
        if rep.structural:
            com_estrutura += 1
        if rep.id_changes:
            com_ids += 1
        if rep.suspects:
            com_suspeitos += 1
        if rep.isolados:
            com_isolados += 1
        for t in rep.slots:
            r, i = agregado.get((t.opcode, t.pi, t.wi), (0, 0))
            agregado[(t.opcode, t.pi, t.wi)] = (r + t.relocados, i + t.instancias)

    total = len(files)
    if sem_par == total:
        print(f"\nNENHUM dos {total} arquivos de {src} tem par em {patched}.")
        if not patched.exists():
            print(f"A pasta {patched} nem existe - confira o caminho do -o que voce usou "
                  f"no inject.")
        else:
            achados = sorted(q for q in patched.rglob("*") if q.is_file())
            if not achados:
                print(f"A pasta {patched} esta vazia: o inject nao gravou nada ali "
                      f"(ou gravou em outro -o).")
            else:
                print(f"O que existe em {patched} ({len(achados)} arquivo(s)):")
                for q in achados[:10]:
                    print(f"    {q.relative_to(patched)}")
                if len(achados) > 10:
                    print(f"    ... e mais {len(achados) - 10}")
                print("Os nomes precisam bater com os originais. Se voce injetou OUTRA pasta "
                      "de scripts, aponte o 'input' para a mesma que usou no inject.")
        return 1
    print(f"\n{total - sujos - sem_par}/{total - sem_par} arquivos mudaram SO o que deviam."
          + (f" ({sem_par} sem par)" if sem_par else ""))
    if sujos:
        print(f"  arquivos com mudanca estrutural ... {com_estrutura}")
        print(f"  arquivos com identificador trocado  {com_ids}")
        print(f"  arquivos com word suspeito ....... {com_suspeitos}")
        print(f"  arquivos com slot isolado ........ {com_isolados}")
    if agregado:
        # a razao do lote inteiro decide melhor que a de um arquivo so: um slot
        # que e ponteiro de verdade fica alto somando 279 arquivos, e uma
        # coincidencia continua rasteira
        linhas = sorted(((r / i if i else 0.0), r, i, k) for k, (r, i) in agregado.items())
        print("\n  relocacao por slot no lote inteiro (opcode, parametro, word):")
        for razao, r, i, (op, pi, wi) in linhas[:args.limit]:
            marca = "  ISOLADO" if razao < 0.25 else ""
            print(f"    0x{op:<8X} param {pi} word {wi}: {r}/{i} = {razao:6.1%}{marca}")
        if len(linhas) > args.limit:
            print(f"    ... e mais {len(linhas) - args.limit} slots (--limit para ver mais)")
        print("  Slot com razao ALTA e ponteiro de verdade. Razao rasteira em MUITOS "
              "arquivos costuma ser ponteiro condicional; rasteira em poucos e coincidencia.")
    if sujos:
        print("\nReinjete com --fit para eliminar a relocacao inteira, ou mande esta saida "
              "para analise antes de aceitar o patch que cresceu.")
    return 1 if sujos else 0


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
            "  0. python stcm2l.py diag    .\\scripts\\algum.DAT   (se extract devolver 0 textos)\n"
            "  2. python stcm2l.py extract .\\scripts -o .\\txt\n"
            "  3. python stcm2l.py translate .\\txt -o .\\txt_ptbr --source JA --cache cache.json\n"
            "     (a fala ja sai quebrada em 50 colunas; use --max-line N ou --max-line 0)\n"
            "  4. (revisao manual dos .json)\n"
            "  5. python stcm2l.py inject  .\\scripts --texts .\\txt_ptbr -o .\\out\n"
            "     (--fit = nenhum bloco muda de tamanho: nenhum ponteiro para errar)\n"
            "  6. python stcm2l.py compare .\\scripts --patched .\\out\n"
            "     (o que mudou alem do texto? identificador trocado e word suspeito)\n"
        ),
    )
    p.add_argument("--version", action="version", version=f"stcm2l-tool {__version__}")
    p.add_argument("--traceback", action="store_true", help="mostra o stack trace completo em erros")
    sub = p.add_subparsers(dest="cmd", required=True)

    def quebra(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--max-line", type=int, default=MAX_LINE_DEFAULT,
                        metavar="N",
                        help=f"quebra a fala traduzida a cada N colunas visiveis "
                             f"para caber na caixa de texto (padrao: {MAX_LINE_DEFAULT}; "
                             f"0 desliga)")
        sp.add_argument("--newline", choices=("auto", "lf", "literal"), default="auto",
                        help="forma da quebra: 'lf' = byte 0x0A, 'literal' = a "
                             "sequencia \\n. Padrao 'auto': deduz do texto original "
                             "do proprio script (confiavel no .json)")

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

    sp = sub.add_parser("diag", help="diagnostica '0 textos': mapa, blocos e busca crua")
    sp.add_argument("input")
    sp.add_argument("--encoding", help="forca a codificacao de leitura (ex.: cp932)")
    sp.add_argument("--limit", type=int, default=25, help="itens mostrados por secao")
    sp.add_argument("--files", type=int, default=2, help="quantos arquivos analisar em lote")
    sp.add_argument("--min-chars", type=int, default=4,
                    help="tamanho minimo de string na varredura crua")
    common(sp)
    sp.set_defaults(func=cmd_diag)

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
    sp.add_argument("--provider",
                    choices=("gtx", "gtx-serial", "deepl", "deepl-pro", "google",
                             "googletrans", "none"),
                    default="gtx",
                    help="gtx (padrao) = endpoint publico do Google EM LOTE, sem chave "
                         "e sem dependencia; gtx-serial = o mesmo uma fala por "
                         "requisicao, so como plano B se o lote for barrado")
    sp.add_argument("--api-key", help="chave da API (DeepL ou Google Cloud Translation)")
    sp.add_argument("--source", help="idioma de origem (JA, EN...). Padrao: deteccao do provedor")
    sp.add_argument("--target", default="PT-BR")
    sp.add_argument("--batch-size", type=int, default=None,
                    help="textos por requisicao (padrao: 500 no gtx, 40 nos demais)")
    sp.add_argument("--retries", type=int, default=3)
    sp.add_argument("--delay", type=float, default=1.0, help="pausa entre lotes, em segundos")
    sp.add_argument("--cache", help="arquivo .json de cache de traducoes (reaproveita repeticoes)")
    sp.add_argument("--overwrite", action="store_true",
                    help="retraduz entradas que ja possuem traducao")
    sp.add_argument("--only-cjk", action="store_true",
                    help="traduz apenas entradas com kana/kanji (scripts em japones)")
    sp.add_argument("--skip-ids", action="store_true",
                    help="traduz fala em qualquer idioma, mas preserva IDs de voz, "
                         "nomes de arquivo e flags do roteiro (scripts em ingles)")
    sp.add_argument("-r", "--recursive", action="store_true")
    quebra(sp)
    sp.set_defaults(func=cmd_translate)

    sp = sub.add_parser("inject", help=".json/.txt -> STCM2L (recalcula os ponteiros)")
    sp.add_argument("input", help="arquivo .DAT original ou pasta com os originais")
    sp.add_argument("--texts", required=True, help="arquivo ou pasta com as traducoes")
    sp.add_argument("-o", "--output", required=True, help="arquivo ou pasta de saida")
    sp.add_argument("--out-encoding",
                    help="codificacao de gravacao (padrao: a mesma do arquivo de traducao)")
    sp.add_argument("--fallback", choices=("strict", "ascii", "replace"), default="strict",
                    help="o que fazer com caracteres que nao cabem na codificacao do jogo")
    sp.add_argument("--fit", action="store_true",
                    help="NAO deixa nenhum bloco mudar de tamanho: o arquivo sai com o mesmo "
                         "layout do original e nenhum ponteiro e recalculado. O que nao couber "
                         "fica em japones e e listado no relatorio. Use quando o jogo se perde "
                         "depois do patch.")
    sp.add_argument("--relocate", choices=("scan", "strict"), default="scan",
                    help="como achar ponteiros quando o texto cresce (ignorado com --fit). "
                         "'scan' reloca todo word que valha um endereco conhecido - pega mais "
                         "ponteiro e tambem mais imediato do jogo por engano; 'strict' so mexe "
                         "no 1o word de cada parametro, exports e collection_link.")
    sp.add_argument("--fix-len", action="store_true",
                    help="atualiza parametros cujo imediato repete o tamanho da string. "
                         "E um palpite: qualquer parametro que por acaso valha o tamanho antigo "
                         "e reescrito junto. Desligado por padrao.")
    sp.add_argument("--no-fix-len", action="store_true",
                    help="(padrao; mantido por compatibilidade com scripts antigos)")
    sp.add_argument("--allow-id-change", action="store_true",
                    help="deixa passar traducao de identificador (id de voz, nome de arquivo, "
                         "label). Por padrao o original e preservado: trocar esses nomes faz o "
                         "jogo nao achar o recurso e voltar para o titulo.")
    sp.add_argument("--strict", action="store_true",
                    help="aborta o arquivo inteiro se o texto original divergir")
    sp.add_argument("--ignore-mismatch", action="store_true",
                    help="injeta mesmo quando o texto original do .json nao bate com o bloco "
                         "do .DAT. Por padrao a entrada e PULADA: divergencia quer dizer que o "
                         "'original' descreve outro bloco, e escrever ali troca uma string pela "
                         "traducao de outra.")
    sp.add_argument("--limit", type=int, default=15, help="avisos mostrados por arquivo")
    quebra(sp)
    common(sp)
    sp.set_defaults(func=cmd_inject)

    sp = sub.add_parser("compare", help="original vs injetado: o que mudou alem do texto?")
    sp.add_argument("input", help="arquivo .DAT original ou pasta com os originais")
    sp.add_argument("--patched", required=True, help="arquivo .DAT injetado ou pasta de saida")
    sp.add_argument("--limit", type=int, default=15, help="itens mostrados por secao")
    sp.add_argument("--show-text", action="store_true",
                    help="lista tambem os textos alterados, nao so os identificadores")
    common(sp)
    sp.set_defaults(func=cmd_compare)

    sp = sub.add_parser("selftest", help="roda a bateria de testes internos")
    sp.set_defaults(func=cmd_selftest)

    return p


def _fix_console() -> None:
    """
    Windows: ao REDIRECIONAR a saida (`> diag.txt`) o Python usa a codificacao
    do locale (cp1252 no Brasil), que nao tem japones - e o programa morre com
    UnicodeEncodeError no meio do relatorio. No console isso nao acontece porque
    o Python escreve via WriteConsoleW. Entao: quando a saida NAO e um terminal,
    forcamos utf-8; e em qualquer caso trocamos o caractere impossivel em vez
    de abortar.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            if stream.isatty():
                reconfigure(errors="replace")
            else:
                reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _fix_console()
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
