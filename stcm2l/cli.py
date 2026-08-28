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
import os
import sys
import traceback
from pathlib import Path

from . import __version__
from .compare import compare
from .core import SlotVerdict, Stcm2lError, parse, slot_verdicts
from .pipeline import (
    DAT_SUFFIXES, Pendencia, extract_file, inject_file, inspect, iter_inputs,
    patch_regua, pendencias, verify_file,
)
from .shorten import (
    ESFORCO_PADRAO, PROVEDORES, ResumoEncurtamento, fazer_chamador,
    geometria_dos_originais, larguras_dos_originais, percentis,
    amostra_de_prosa, perfil_dos_originais, piso_e_teto, relatorio_seco,
    shorten_entries,
)
from .textio import (
    FOLGA_DEFAULT, MAX_LINE_DEFAULT, MAX_LINES_DEFAULT, PISO_PERCENTIL,
    dump_entries, load_entries,
)
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
            distintos, vistos = [], {}
            for enc, txt in exemplo.items():
                if txt in vistos:
                    # a escolhida nunca some por deduplicacao: ela carrega as
                    # outras que leem igual (texto ASCII le igual em todas)
                    if enc == nfo.encoding:
                        i_ant = vistos[txt]
                        distintos[i_ant] = (f"{nfo.encoding} (={distintos[i_ant][0]})", txt)
                    continue
                vistos[txt] = len(distintos)
                distintos.append((enc, txt))
            for enc, txt in distintos:
                marca = "->" if enc.startswith(nfo.encoding) else "  "
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
    overflow_linhas = 0
    overflow_largura = 0
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
                max_line=args.max_line, max_lines=args.max_lines,
                newline=args.newline,
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
        overflow_linhas += rep.overflow_linhas
        overflow_largura += rep.overflow_largura
        divergentes += rep.divergentes
        id_changes += rep.id_changes
        extra = ""
        if rep.overflow:
            extra += f", {rep.overflow} NAO couberam"
        if rep.id_changes:
            extra += f", {rep.id_changes} identificadores"
        if rep.overflow_linhas or rep.overflow_largura:
            extra += f", {max(rep.overflow_linhas, rep.overflow_largura)} passam da caixa"
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
    if overflow_linhas or overflow_largura:
        print(f"{overflow_largura} fala(s) mais largas que o original permite e "
              f"{overflow_linhas} passando das {args.max_lines} linhas - o jogo vai cortar "
              f"na tela. Rode 'stcm2l shorten' nos .json antes de injetar.")
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
    vereditos_lote: dict[tuple[int, int, int], str] = {}
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
        if args.summary:
            if not rep.clean:
                motivos = []
                if rep.structural:
                    motivos.append(f"{len(rep.structural)} estrutura")
                if rep.id_changes:
                    motivos.append(f"{len(rep.id_changes)} identificador")
                if rep.suspects:
                    motivos.append(f"{len(rep.suspects)} suspeito")
                if rep.realinhados:
                    motivos.append(f"{len(rep.realinhados)} realinhado")
                if rep.isolados:
                    motivos.append(f"{len(rep.isolados)} slot isolado")
                print(f"[!!!!] {path.name}: {', '.join(motivos)}")
        else:
            print(f"\n[{marca}] {path.name}")
        if args.summary:
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
            for chave, vd in rep.vereditos.items():
                vereditos_lote.setdefault(chave, vd)
                if vd == "ponteiro":
                    vereditos_lote[chave] = vd
            continue
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
        real = rep.realinhados
        if real:
            print(f"       {len(real)} word(s) reescritos para endereco VALIDO do injetado, "
                  f"mas o alvo logico nao pode ser conferido (o elemento re-segmentou). "
                  f"O ponteiro esta bom; a conferencia e que nao alcanca.")
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
        for chave, vd in rep.vereditos.items():
            vereditos_lote.setdefault(chave, vd)
            if vd == "ponteiro":
                vereditos_lote[chave] = vd

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
        print("    razao = relocadas/instancias | veredito = acertos/CANDIDATAS, "
              "medido nos originais")
        for razao, r, i, (op, pi, wi) in linhas[:args.limit]:
            vd = vereditos_lote.get((op, pi, wi), "?")
            if vd == "ponteiro":
                marca = "  ponteiro (relocacao correta)"
            elif razao < 0.25:
                marca = f"  ISOLADO + {vd}"
            else:
                marca = f"  {vd}"
            print(f"    0x{op:<8X} param {pi} word {wi}: {r}/{i} = {razao:6.1%}{marca}")
        if len(linhas) > args.limit:
            print(f"    ... e mais {len(linhas) - args.limit} slots (--limit para ver mais)")
        print("  'ponteiro' = quase toda candidata acerta um endereco: relocar esta certo, "
              "mesmo com razao baixa.\n"
              "  'acaso'    = acerta na proporcao do acaso: e imediato do jogo sendo "
              "reescrito. Use --relocate slots.")
    if sujos:
        print("\nReinjete com --fit para eliminar a relocacao inteira, ou mande esta saida "
              "para analise antes de aceitar o patch que cresceu.")
    return 1 if sujos else 0


def cmd_slots(args: argparse.Namespace) -> int:
    """Quais words de parametro sao MESMO ponteiro, medido nos originais."""
    files = iter_inputs(Path(args.input), args.recursive, args.suffixes)
    if not files:
        print("nenhum arquivo encontrado.")
        return 1
    somado: dict[tuple[int, int, int], list[int]] = {}
    densidades: list[float] = []
    lidos = 0
    for path in files:
        try:
            script = parse(path.read_bytes())
        except Stcm2lError as exc:
            print(f"[ERRO] {path.name}: {exc}")
            continue
        lidos += 1
        for chave, v in slot_verdicts(script).items():
            acc = somado.setdefault(chave, [0, 0, 0])
            acc[0] += v.instancias
            acc[1] += v.candidatas
            acc[2] += v.acertos
            if v.densidade:
                densidades.append(v.densidade)
    if not lidos:
        return 1
    densidade = sum(densidades) / len(densidades) if densidades else 0.0
    print(f"\n{lidos} arquivo(s). Densidade media de enderecos entre as posicoes "
          f"4-alinhadas: {densidade:.1%}")
    print("  = a chance de um numero QUALQUER cair sobre um endereco por acaso.\n")
    print(f"  {'opcode':>10} {'p':>2} {'w':>2} {'instancias':>11} {'candidatas':>11} "
          f"{'acertos':>8} {'precisao':>9}  veredito")

    linhas = []
    for (op, pi, wi), (inst, cand, acer) in somado.items():
        if not cand:
            continue
        v = SlotVerdict(op, pi, wi, inst, cand, acer, densidade)
        linhas.append((v.veredito != "ponteiro", -v.precisao, v))
    for _, _, v in sorted(linhas, key=lambda x: (x[0], x[1]))[:args.limit]:
        marca = {"ponteiro": "PONTEIRO", "acaso": "acaso (imediato do jogo)",
                 "duvidoso": "duvidoso"}[v.veredito]
        print(f"  0x{v.opcode:<8X} {v.pi:>2} {v.wi:>2} {v.instancias:>11} "
              f"{v.candidatas:>11} {v.acertos:>8} {v.precisao:>8.1%}  {marca}")
    total = len(linhas)
    if total > args.limit:
        print(f"  ... e mais {total - args.limit} slots (--limit para ver mais)")

    ponteiros = sum(1 for _, _, v in linhas if v.veredito == "ponteiro")
    acasos = sum(1 for _, _, v in linhas if v.veredito == "acaso")
    duvidas = total - ponteiros - acasos
    print(f"\n  {ponteiros} slots sao ponteiro, {acasos} sao acaso, {duvidas} duvidosos.")
    if acasos:
        print("  Com --relocate scan (o padrao) os 'acaso' sao reescritos quando o texto "
              "cresce, e e assim que o roteiro quebra sem erro nenhum.")
        print("  Use 'inject --relocate slots': ele reloca so os PONTEIRO.")
    return 0


def cmd_pending(args: argparse.Namespace) -> int:
    """O que sobrou sem traduzir no .DAT ja injetado - e por que."""
    src = Path(args.input)
    files = iter_inputs(src, args.recursive, args.suffixes)
    if not files:
        print("nenhum arquivo encontrado.")
        return 1
    texts = Path(args.texts) if args.texts else None
    total_cjk = total_prose = total_blocos = 0
    motivos: dict[str, int] = {}
    amostras: list[tuple[str, Pendencia]] = []
    for path in files:
        par = _pair_texts(path, texts) if texts else None
        try:
            pend, blocos = pendencias(path, par, args.encoding)
        except Stcm2lError as exc:
            print(f"[ERRO] {path.name}: {exc}")
            continue
        total_blocos += blocos
        cjk = [q for q in pend if q.kind == "cjk"]
        prosa = [q for q in pend if q.kind == "prose"]
        total_cjk += len(cjk)
        total_prose += len(prosa)
        for q in pend:
            motivos[q.motivo or "(sem arquivo de traducao)"] = \
                motivos.get(q.motivo or "(sem arquivo de traducao)", 0) + 1
        if cjk and len(amostras) < args.limit:
            amostras.extend((path.name, q) for q in cjk[:3])
        if cjk and not args.quiet:
            print(f"[{len(cjk):>5} JA] {path.name}"
                  + (f"  (+{len(prosa)} em outro idioma)" if prosa else ""))

    print(f"\n{total_blocos} blocos de texto nos arquivos injetados.")
    print(f"  ainda em japones ........ {total_cjk}")
    print(f"  em outro idioma latino .. {total_prose}  (pode ser ingles nao traduzido)")
    if motivos:
        print("\n  motivo:")
        for motivo, n in sorted(motivos.items(), key=lambda kv: -kv[1]):
            print(f"    {n:>7}  {motivo}")
    if amostras:
        print("\n  exemplos do que ficou em japones:")
        for nome, q in amostras[:args.limit]:
            corte = q.texto if len(q.texto) <= 46 else q.texto[:46] + "..."
            print(f"    {nome} [{q.entry_id}] {corte!r}  <- {q.motivo}")
    if not texts:
        print("\n  (passe --texts <pasta> para saber o MOTIVO de cada pendencia)")
    return 0


def cmd_shorten(args: argparse.Namespace) -> int:
    """Encurta o que nao cabe na caixa de texto do jogo."""
    src = Path(args.input)
    files = iter_inputs(src, args.recursive, TEXT_SUFFIXES)
    if not files:
        print("nenhum arquivo de texto (.json/.txt) encontrado.")
        return 1
    batch = src.is_dir()

    # --dry-run nao chama a API: primeiro se mede o gasto, depois se gasta
    if args.dry_run:
        total = 0
        n_entradas = n_traduzidas = por_largura = por_linhas = 0
        deficit: dict[str, int] = {}
        pisos: list[tuple[str, int, int]] = []
        larguras: list[int] = []
        por_linha: list[int] = []
        linhas_orig: dict[int, int] = {}
        perfil: dict[str, int] = {}
        amostras_prosa: list[tuple[str, str]] = []
        faixas = ((10, "1-10 colunas"), (25, "11-25 colunas"),
                  (50, "26-50 colunas"), (10 ** 9, "50+ colunas"))
        for path in files:
            entries, _ = load_entries(path)
            n_entradas += len(entries)
            n_traduzidas += sum(1 for e in entries if e.translation.strip())
            piso, teto = piso_e_teto(entries, args.max_line, args.max_lines,
                                     args.percentil)
            larguras.extend(larguras_dos_originais(entries))
            for k, v in perfil_dos_originais(entries).items():
                perfil[k] = perfil.get(k, 0) + v
            if len(amostras_prosa) < 6:
                amostras_prosa.extend((path.name, t)
                                      for t in amostra_de_prosa(entries, 2))
            lg, ct = geometria_dos_originais(entries)
            por_linha.extend(lg)
            for k, v in ct.items():
                linhas_orig[k] = linhas_orig.get(k, 0) + v
            pend = relatorio_seco(entries, args.max_line, args.max_lines,
                                  args.newline, args.width_slack, args.percentil,
                                  args.width_tolerance, not args.no_original_budget)
            total += len(pend)
            if pend:
                pisos.append((path.name, piso, teto))
            for e, linhas, colunas, orc in pend:
                falta = colunas - orc if orc else 0
                if falta > 0:
                    por_largura += 1
                    for limite, rotulo in faixas:
                        if falta <= limite:
                            deficit[rotulo] = deficit.get(rotulo, 0) + 1
                            break
                else:
                    por_linhas += 1
            if pend and not args.quiet:
                print(f"[{len(pend):>5}] {path.name}  (piso {piso}, teto {teto})")
                for e, linhas, colunas, orc in pend[:args.limit]:
                    falta = colunas - orc if orc else 0
                    print(f"        [{e.id}] {colunas} colunas / orcamento {orc}"
                          + (f" -> cortar {falta}" if falta > 0 else "")
                          + f" | {linhas} linhas: {e.translation[:50]!r}")

        print(f"\n{len(files)} arquivo(s), {n_entradas} entradas, "
              f"{n_traduzidas} com traducao.")
        if deficit:
            print("\n  quanto falta cortar de cada fala candidata:")
            for _, rotulo in faixas:
                if rotulo in deficit:
                    dica = ("   <- barato, considere --width-tolerance"
                            if rotulo.startswith("1-10") else "")
                    print(f"    {rotulo:>14}: {deficit[rotulo]:>7}{dica}")
        if perfil:
            fala = perfil.get("cjk", 0) + perfil.get("prose", 0)
            print(f"\n  o que esta no campo 'original': {perfil.get('cjk', 0)} japones, "
                  f"{perfil.get('prose', 0)} prosa latina, {perfil.get('id', 0)} identificador")
            if amostras_prosa:
                print("  exemplos do que caiu como prosa latina:")
                for nome, t in amostras_prosa[:6]:
                    print(f"    {nome}: {t[:60]!r}")
                print("    -> se isso for PORTUGUES, o .json esta contaminado e precisa "
                      "ser re-extraido dos .DAT limpos.")
                print("    -> se for ingles/nome de recurso, sempre esteve ai e nao ha "
                      "problema nenhum.")
        if larguras:
            larguras.sort()
            pc = percentis(larguras)
            print(f"\n  largura das {len(larguras)} falas ORIGINAIS (e a caixa que o "
                  f"jogo comprovadamente desenhou):")
            print("    " + "  ".join(f"{k}={v}" for k, v in pc.items()))
            print(f"    o piso sai do P{args.percentil}. Se a massa esta bem acima dele, "
                  f"suba com --percentil.")
        if por_linha:
            por_linha.sort()
            pl = percentis(por_linha)
            print(f"\n  largura de cada LINHA do original (a LARGURA da caixa):")
            print("    " + "  ".join(f"{k}={v}" for k, v in pl.items()))
            total_o = sum(linhas_orig.values())
            print(f"  quantas linhas o original ja usava (a ALTURA da caixa):")
            for n in sorted(linhas_orig):
                print(f"    {n} linha(s): {linhas_orig[n]:>7} "
                      f"({linhas_orig[n] * 100 // max(total_o, 1)}%)")
            print(f"    -> a caixa comporta ao menos {pl['max']} colunas x "
                  f"{max(linhas_orig)} linhas, porque o jogo ja desenhou isso.")
        if pisos:
            print(f"\n  piso do lote (P{args.percentil} dos originais): "
                  + ", ".join(f"{n}={p}" for n, p, _ in pisos[:3])
                  + (" ..." if len(pisos) > 3 else "")
                  + f" | teto da caixa: {pisos[0][2]}")
        print(f"\n{total} falas estouram: {por_largura} por LARGURA "
              f"(orcamento do original +{args.width_slack:.0%}), "
              f"{por_linhas} por LINHAS ({args.max_lines} de {args.max_line} colunas).")
        if total:
            print(f"Rode sem --dry-run para encurtar. Estimativa: "
                  f"~{total // args.batch_size + 1} requisicoes em lotes de "
                  f"{args.batch_size}.")
        elif not n_traduzidas:
            print("Nenhuma entrada tem traducao: nao havia o que medir. O --texts "
                  "aponta para os .json EXTRAIDOS em vez dos TRADUZIDOS?")
        return 0

    if not args.output:
        print("faltou -o/--output (ou use --dry-run, que nao grava nada).")
        return 2
    out = Path(args.output)
    if batch:
        out.mkdir(parents=True, exist_ok=True)

    api_key = args.api_key or os.environ.get(
        "GEMINI_API_KEY" if args.ai_provider == "gemini" else "ANTHROPIC_API_KEY")
    try:
        chamar = fazer_chamador(args.ai_provider, args.ai_model, api_key, args.ai_effort)
    except Stcm2lError as exc:
        print(f"ERRO: {exc}")
        return 2

    total = ResumoEncurtamento()
    cache = Path(args.cache) if args.cache else None
    for path in files:
        entries, meta = load_entries(path)
        print(f"\n-> {path.name}")
        try:
            rep = shorten_entries(
                entries, chamar, max_line=args.max_line, max_lines=args.max_lines,
                newline=args.newline, batch_size=args.batch_size,
                retries=args.retries, folga=args.width_slack,
                percentil=args.percentil, tolerancia=args.width_tolerance,
                usar_original=not args.no_original_budget, cache_path=cache,
                log=lambda m: print(m),
            )
        except Stcm2lError as exc:
            print(f"   ERRO: {exc}")
            return 2
        target = _out_for(path, out, path.suffix, batch, src if batch else None)
        fmt = "json" if target.suffix.lower() == ".json" else "txt"
        dump_entries(entries, target, meta.get("source", path.stem),
                     meta.get("encoding", "utf-8"), fmt)
        print(f"   {rep.candidatas} estouravam -> {rep.resolvidas} resolvidas "
              f"({rep.resolvidas_2} precisaram de resumo), {rep.restantes} para revisar"
              + (f", {rep.do_cache} do cache" if rep.do_cache else "")
              + f" -> {target}")
        for ex in rep.exemplos[:args.limit]:
            print(f"     [{ex.id}] {ex.colunas_antes} -> {ex.colunas_depois} colunas "
                  f"(orcamento {ex.orcamento})  |  {ex.linhas_antes} -> "
                  f"{ex.linhas_depois} linhas")
            print(f"       antes : {ex.antes[:70]!r}")
            print(f"       depois: {ex.depois.replace(chr(10), ' / ')[:70]!r}")
            if ex.faltam > 0:
                print(f"       ainda faltam {ex.faltam} colunas")
        for campo in ("candidatas", "resolvidas_1", "resolvidas_2", "restantes",
                      "marcadores_perdidos", "do_cache", "por_largura", "por_linhas"):
            setattr(total, campo, getattr(total, campo) + getattr(rep, campo))

    print(f"\n{total.resolvidas}/{total.candidatas} falas passaram a caber "
          f"({total.por_largura} eram de largura, {total.por_linhas} de linhas).")
    if total.restantes:
        print(f"{total.restantes} continuam estourando e sairam com needs_review - "
              f"encurte essas a mao no .json.")
    if total.marcadores_perdidos:
        print(f"{total.marcadores_perdidos} perderam marcadores no caminho e foram "
              f"marcadas para revisao.")
    return 0


def cmd_ruler(args: argparse.Namespace) -> int:
    """Injeta reguas de medicao para ler a caixa de texto no jogo."""
    src = Path(args.input)
    out = Path(args.output)
    if out.resolve() == src.resolve():
        print("ERRO: a saida sobrescreveria o original. Use outro caminho.")
        return 1
    try:
        trocadas = patch_regua(src, out, args.count, args.encoding,
                               inicio=args.start, todas=args.all)
    except Stcm2lError as exc:
        print(f"[ERRO] {src.name}: {exc}")
        return 1
    if not trocadas:
        print(f"{src.name}: nenhuma fala encontrada para trocar pela regua.")
        return 1
    print(f"[ OK ] {src.name}: {len(trocadas)} falas trocadas por regua -> {out}\n")
    for eid, regua in trocadas[:8]:
        print(f"  [{eid}] {regua!r}")
    if len(trocadas) > 8:
        print(f"  ... e mais {len(trocadas) - 8} falas")
    print("\nColoque este arquivo no jogo e leia na tela:")
    print("  1. a regua de COLUNAS corta em que numero? -> esse e o --max-line real")
    print("  2. a regua de LINHAS mostra ate qual L? -> esse e o --max-lines real")
    print("  3. as linhas L1..L5 aparecem separadas? Se sairem TODAS emendadas numa")
    print("     linha so, a engine ignora a nossa quebra e quebra sozinha - ai o")
    print("     criterio passa a ser comprimento total, nao numero de linhas.")
    return 0


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
        sp.add_argument("--max-lines", type=int, default=MAX_LINES_DEFAULT, metavar="N",
                        help="quantas linhas a caixa de texto do jogo aguenta (0 desliga a "
                             "conferencia). Estourar isso corta a fala na tela.")
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
    sp.add_argument("--relocate", choices=("scan", "strict", "slots"), default="scan",
                    help="como achar ponteiros quando o texto cresce (ignorado com --fit). "
                         "'slots' MEDE quais slots (opcode, parametro, word) sao ponteiro e "
                         "so reloca esses - e o seguro para deixar o texto crescer; "
                         "'scan' reloca todo word que valha um endereco conhecido, pegando "
                         "tambem imediato do jogo por engano; 'strict' so mexe no 1o word de "
                         "cada parametro, exports e collection_link. Confira com 'slots'.")
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
    sp.add_argument("--summary", action="store_true",
                    help="so o veredito: uma linha por arquivo problematico e o resumo do "
                         "lote no fim. Para varrer uma arvore inteira sem afogar o terminal.")
    common(sp)
    sp.set_defaults(func=cmd_compare)

    sp = sub.add_parser("ruler", help="injeta reguas para MEDIR a caixa de texto no jogo")
    sp.add_argument("input", help="um .DAT do comeco do jogo (prologo)")
    sp.add_argument("-o", "--output", required=True, help=".DAT de saida com as reguas")
    sp.add_argument("--count", type=int, default=6, help="quantas falas trocar")
    sp.add_argument("--start", type=int, default=0,
                    help="pula as N primeiras falas antes de comecar a trocar")
    sp.add_argument("--all", action="store_true",
                    help="troca TODAS as falas do arquivo. Use quando a regua 'nao "
                         "apareceu': o comeco do arquivo e cheio de identificador, entao "
                         "as primeiras falas de verdade costumam estar la no fim. Deixa o "
                         "arquivo ilegivel de proposito - e build de medicao.")
    sp.add_argument("--encoding", help="forca a codificacao de leitura")
    sp.set_defaults(func=cmd_ruler)

    sp = sub.add_parser("shorten", help="encurta com IA a fala que nao cabe na caixa")
    sp.add_argument("input", help="arquivo .json/.txt traduzido ou pasta com eles")
    sp.add_argument("-o", "--output", help="arquivo ou pasta de saida (nao precisa com --dry-run)")
    sp.add_argument("--dry-run", action="store_true",
                    help="so conta o que estoura, sem chamar a API nem gravar. Rode isto "
                         "primeiro: dimensiona o gasto antes de gastar.")
    sp.add_argument("--ai-provider", choices=PROVEDORES, default="gemini")
    sp.add_argument("--ai-model", help="nome do modelo (padrao: o do provedor). Nome errado "
                                       "faz o comando listar os disponiveis.")
    sp.add_argument("--ai-effort", default=ESFORCO_PADRAO,
                    help="esforco do modelo (so no provedor claude)")
    sp.add_argument("--api-key", help="credencial. Sem isto, sai de GEMINI_API_KEY / "
                                      "ANTHROPIC_API_KEY do ambiente.")
    sp.add_argument("--batch-size", type=int, default=25, help="falas por requisicao")
    sp.add_argument("--retries", type=int, default=3)
    sp.add_argument("--width-slack", type=float, default=FOLGA_DEFAULT, metavar="F",
                    help=f"folga sobre a largura do original (padrao {FOLGA_DEFAULT * 100:.0f}%%). "
                         f"Portugues e mais comprido que japones; exigir paridade exata "
                         f"espreme demais.")
    sp.add_argument("--percentil", type=int, default=PISO_PERCENTIL, metavar="N",
                    help=f"percentil das larguras dos originais usado como PISO do "
                         f"orcamento (padrao {PISO_PERCENTIL}). Original curto nao prova "
                         f"caixa estreita, so que a caixa mostra pelo menos aquilo.")
    sp.add_argument("--width-tolerance", type=int, default=0, metavar="N",
                    help="nao vira candidata por menos de N colunas de estouro. Evita "
                         "pagar uma requisicao para cortar 3 colunas.")
    sp.add_argument("--no-original-budget", action="store_true",
                    help="ignora a largura do original e usa so o orcamento de caixa "
                         "(--max-line x --max-lines), como era antes.")
    sp.add_argument("--cache", help="arquivo .json de cache (re-rodar sai de graca)")
    sp.add_argument("--limit", type=int, default=5, help="exemplos mostrados por arquivo")
    sp.add_argument("-q", "--quiet", action="store_true",
                    help="no --dry-run, so o resumo (sem a lista por arquivo)")
    sp.add_argument("-r", "--recursive", action="store_true")
    quebra(sp)
    sp.set_defaults(func=cmd_shorten)

    sp = sub.add_parser("pending", help="o que sobrou sem traduzir no .DAT injetado, e por que")
    sp.add_argument("input", help="arquivo .DAT INJETADO ou pasta com a saida do inject")
    sp.add_argument("--texts", help="pasta dos .json traduzidos (revela o MOTIVO de cada caso)")
    sp.add_argument("--encoding", help="forca a codificacao de leitura")
    sp.add_argument("--limit", type=int, default=15, help="exemplos mostrados")
    sp.add_argument("-q", "--quiet", action="store_true", help="so o resumo, sem a lista por arquivo")
    common(sp)
    sp.set_defaults(func=cmd_pending)

    sp = sub.add_parser("slots", help="quais words de parametro sao MESMO ponteiro")
    sp.add_argument("input", help="arquivo .DAT ORIGINAL ou pasta com os originais")
    sp.add_argument("--limit", type=int, default=25, help="slots mostrados")
    common(sp)
    sp.set_defaults(func=cmd_slots)

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
