"""
stcm2l.selftest
===============

Gera arquivos STCM2L sinteticos (com ponteiros reais) e valida o ciclo completo:
parse -> build byte-a-byte identico -> extrair -> traduzir -> injetar -> reabrir
-> conferir se TODOS os ponteiros continuam apontando para o lugar certo.

E o mesmo teste que voce deve rodar contra os .DAT reais do jogo com
`stcm2l.py verify` antes de comecar a traduzir.
"""

from __future__ import annotations

import struct
import tempfile
from pathlib import Path

from .core import (
    slot_verdicts,
    DATA_HEADER_SIZE, EXPORT_ENTRY_SIZE, EXPORT_NAME_SIZE, HEADER_SIZE, align4,
    build, parse, roundtrip_check,
)
from .pipeline import collect_entries, inject_file, pendencias
from .compare import compare
from .textio import (
    TextEntry,
    classify_text, detect_encoding, dump_entries, load_entries, protect_tags, wrap_text,
)


def _data_block(text: str, encoding: str) -> bytes:
    raw = text.encode(encoding) + b"\x00"
    padded = align4(len(raw))
    payload = raw + b"\x00" * (padded - len(raw))
    return struct.pack("<4I", 0, 1, padded, len(raw)) + payload


def make_sample(texts: list[str], encoding: str = "utf-8",
                magic: bytes = b"STCM2L Demangled Ver1.00") -> bytes:
    """Monta um .DAT sintetico valido, com ponteiros absolutos reais."""
    code_start = b"CODE_START_".ljust(16, b"\x00")
    code_end = b"CODE_END_".ljust(16, b"\x00")
    global_tag = b"GLOBAL_DATA".ljust(16, b"\x00")
    global_block = _data_block("SYSTEM_GLOBAL", "ascii")

    # --- passo 1: geometria -------------------------------------------------
    offsets: list[int] = []
    bodies: list[bytes] = []
    pos = HEADER_SIZE + len(code_start)
    for text in texts:
        blk = _data_block(text, encoding)
        head = 16 + 2 * 12                       # 2 parametros
        offsets.append(pos)
        bodies.append(blk)
        pos += head + len(blk)
    code_end_off = pos
    global_tag_off = code_end_off + len(code_end)
    global_block_off = global_tag_off + len(global_tag)
    export_off = align4(global_block_off + len(global_block))

    # --- passo 2: serializacao com ponteiros resolvidos ----------------------
    out = bytearray()
    out += magic.ljust(0x20, b"\x00")
    out += struct.pack("<4I", export_off, 1, global_tag_off, 0)
    out += code_start
    for i, (off, blk) in enumerate(zip(offsets, bodies)):
        ptr = off + 16 + 2 * 12                  # bloco de dado logo apos os params
        total = 16 + 2 * 12 + len(blk)
        out += struct.pack("<4I", 0, 0x30 + i, 2, total)
        out += struct.pack("<3I", ptr, 0, 0)                      # param 0: ponteiro
        out += struct.pack("<3I", 0xFFFFFFFF, 0xFFFFFFFF, i)      # param 1: imediato
        out += blk
    out += code_end
    out += global_tag
    out += global_block
    out += b"\x00" * (export_off - len(out))
    out += b"MAIN".ljust(0x20, b"\x00") + struct.pack("<2I", offsets[0], 0)
    return bytes(out)


def _wordcount_block(text: str, encoding: str) -> bytes:
    """Bloco no layout (0, padded/4, 1, padded) - build 'STCM2L Apr 22 2013'."""
    raw = text.encode(encoding) + b"\x00"
    padded = align4(len(raw))
    payload = raw + b"\x00" * (padded - len(raw))
    return struct.pack("<4I", 0, padded // 4, 1, padded) + payload


def make_otome_sample(inline: list[str], pool: list[str],
                      encoding: str = "cp932") -> bytes:
    """
    Amostra no formato das VNs da Otomate: SEM CODE_END_, blocos wordcount
    embutidos nas acoes e um pool de strings DEPOIS da tabela de exports,
    alcancado pelo primeiro parametro de cada acao.
    """
    def corpo(ptrs: list[int]) -> tuple[bytes, list[int]]:
        buf = bytearray(b"CODE_START_".ljust(16, b"\x00"))
        offsets = []
        for i, linha in enumerate(inline):
            blk = _wordcount_block(linha, encoding)
            offsets.append(HEADER_SIZE + len(buf))
            buf += struct.pack("<4I", 0, 0x4BA + (i % 3), 1, 16 + 12 + len(blk))
            buf += struct.pack("<3I", ptrs[i % len(ptrs)] if ptrs else 0,
                               0x40000000, 0x40000000)
            buf += blk
        return bytes(buf), offsets

    body, offsets = corpo([0])
    export_off = align4(HEADER_SIZE + len(body))
    pos = export_off + len(pool) * EXPORT_ENTRY_SIZE
    ptrs = []
    for t in pool:
        ptrs.append(pos)
        pos += len(_wordcount_block(t, encoding))
    body, offsets = corpo(ptrs)              # 2o passe, ja com os ponteiros certos

    out = bytearray(b"STCM2L Apr 22 2013 19:39:01".ljust(0x20, b"\x00"))
    out += struct.pack("<4I", export_off, len(pool), 1, 0)
    out += body
    out += b"\x00" * (export_off - len(out))
    for i, _ in enumerate(pool):
        out += (b"EXP%d" % i).ljust(EXPORT_NAME_SIZE, b"\x00")
        out += struct.pack("<2I", offsets[i % len(offsets)], 0)
    for t in pool:
        out += _wordcount_block(t, encoding)
    return bytes(out)


def _check_otome(tmpdir: Path, log) -> bool:
    """Cobre o formato Otomate de ponta a ponta: extrair, injetar maior, ponteiros."""
    inline = ["はあ……。", "なんでこんなことになったんだろう。", "#Name[2]", "転校!?"]
    pool = ["（お父さんの仕事の都合で）", "（ヴァンパイアって……夢でも）"]
    dat = tmpdir / "otome.DAT"
    dat.write_bytes(make_otome_sample(inline, pool))
    ok = True

    same, detail = roundtrip_check(dat.read_bytes())
    log(f"[18] otome: round-trip byte-a-byte: {'OK' if same else 'FALHOU'} ({detail})")
    ok &= same

    script = parse(dat.read_bytes())
    entries = collect_entries(script, "cp932")
    got = [e.original for e in entries]
    text_ok = got == inline + pool
    log(f"[19] otome: {len(entries)} textos extraidos "
        f"({'OK' if text_ok else 'FALHOU'}) - inclui o pool da cauda")
    if not text_ok:
        log(f"     esperado={inline + pool}\n     obtido  ={got}")
    ok &= text_ok

    for i, e in enumerate(entries):
        e.translation = f"Linha {i} traduzida com um tamanho bem maior que o original."
    texts_file = tmpdir / "otome.json"
    dump_entries(entries, texts_file, dat.name, "utf-8", "json")
    out_dat = tmpdir / "out" / "otome.DAT"
    report = inject_file(dat, texts_file, out_dat, out_encoding="utf-8")
    ids = [e for e in entries if classify_text(e.original) == "id"]
    inj_ok = (report.applied == len(entries) - len(ids)
              and not any(p.startswith("ERRO") for p in report.problems))
    log(f"[20] otome: injecao: {report.applied} aplicados, "
        f"{report.grown} maiores ({'OK' if inj_ok else 'FALHOU'})")
    ok &= inj_ok

    new = parse(out_dat.read_bytes())
    got = [e.original for e in collect_entries(new, "utf-8")]
    want = [e.original if classify_text(e.original) == "id"
            else wrap_text(e.translation) for e in entries]
    text_ok = got == want
    log(f"[21] otome: textos apos injecao (ja quebrados em 50): "
        f"{'OK' if text_ok else 'FALHOU'}")
    ok &= text_ok

    # os ponteiros para o pool da cauda tem que seguir o pool que se moveu
    destinos = {}
    for ei, si, db in new.iter_data_blocks():
        el = new.elements[ei]
        destinos[el.offset + el.segment_rel(si)] = db.content.decode("utf-8")
    esperado = [wrap_text(e.translation) for e in entries[len(inline):]]
    esperado += [e.original for e in entries if classify_text(e.original) == "id"]
    apontados = []
    for el in new.elements:
        if el.kind == "action" and el.params and el.params[0][0]:
            apontados.append(destinos.get(el.params[0][0]))
    ptr_ok = bool(apontados) and all(a in esperado for a in apontados)
    log(f"[22] otome: ponteiros para o pool da cauda: {'OK' if ptr_ok else 'FALHOU'}")
    if not ptr_ok:
        log(f"     apontados={apontados}\n     esperado={esperado}")
    ok &= ptr_ok
    return bool(ok)


def _check_fit(tmpdir: Path, log) -> bool:
    """
    Modo --fit: nenhum bloco muda de tamanho, entao o arquivo sai com o MESMO
    layout e nenhum ponteiro precisa ser recalculado. E o modo seguro para
    quando o jogo comeca a se perder depois do patch.
    """
    ok = True
    inline = ["はあ……。", "なんでこんなことになったんだろう。", "#Name[2]", "転校!?"]
    pool = ["（お父さんの仕事の都合で）", "（ヴァンパイアって……夢でも）"]
    dat = tmpdir / "fit.DAT"
    dat.write_bytes(make_otome_sample(inline, pool))
    original = dat.read_bytes()

    script = parse(original)
    entries = collect_entries(script, "cp932")
    # metade cabe folgada, metade estoura de proposito
    curtas, longas = [], []
    for i, e in enumerate(entries):
        if classify_text(e.original) == "id":
            continue
        if i % 2 == 0:
            e.translation = "Ah..."
            curtas.append(e)
        else:
            e.translation = ("Uma traducao propositalmente muito maior do que o "
                             "bloco original comporta, so para estourar.")
            longas.append(e)
    texts_file = tmpdir / "fit.json"
    dump_entries(entries, texts_file, dat.name, "utf-8", "json")

    out = tmpdir / "out" / "fit.DAT"
    rep = inject_file(dat, texts_file, out, out_encoding="utf-8", fit=True)
    saida = out.read_bytes()
    tam_ok = len(saida) == len(original) and rep.layout_preserved
    conta_ok = rep.applied == len(curtas) and rep.overflow == len(longas)
    log(f"[23] fit: tamanho {len(original)} -> {len(saida)} "
        f"({'OK' if tam_ok else 'FALHOU'}); {rep.applied} aplicados, "
        f"{rep.overflow} recusados por nao caber ({'OK' if conta_ok else 'FALHOU'})")
    ok &= tam_ok and conta_ok

    # todo offset de elemento tem que estar onde estava
    novo = parse(saida)
    offs_ok = ([e.offset for e in novo.elements] == [e.offset for e in script.elements]
               and novo.header.export_offset == script.header.export_offset
               and novo.header.collection_link == script.header.collection_link)
    log(f"[24] fit: todos os offsets e ponteiros intactos: {'OK' if offs_ok else 'FALHOU'}")
    ok &= offs_ok

    # o que nao coube continua em japones; o que coube foi trocado
    def _conteudo(sc, entry):
        return sc.elements[entry.elem].segments[entry.seg].content

    # o que estourou continua com os bytes japoneses ORIGINAIS, byte a byte
    manteve = all(_conteudo(novo, e) == _conteudo(script, e) for e in longas)
    trocou = all(_conteudo(novo, e).decode("utf-8").rstrip() == e.translation
                 for e in curtas)
    log(f"[25] fit: o que nao coube ficou em japones ({'OK' if manteve else 'FALHOU'}) "
        f"e o que coube foi traduzido ({'OK' if trocou else 'FALHOU'})")
    ok &= manteve and trocou

    # -- compare -------------------------------------------------------------
    rel = compare(dat, out)
    cmp_ok = (not rel.words and not rel.structural and not rel.id_changes
              and len(rel.texts) == len(curtas))
    log(f"[26] compare(original, fit): {len(rel.texts)} textos, {len(rel.words)} words "
        f"reescritos, {len(rel.suspects)} suspeitos ({'OK' if cmp_ok else 'FALHOU'})")
    ok &= cmp_ok

    # crescendo: words REALMENTE mudam, mas todos batem com a relocacao logica
    for e in entries:
        if classify_text(e.original) != "id":
            e.translation = f"Traducao {e.id} bem maior do que o original japones."
    big_texts = tmpdir / "fit_big.json"
    dump_entries(entries, big_texts, dat.name, "utf-8", "json")
    big = tmpdir / "out" / "fit_big.DAT"
    inject_file(dat, big_texts, big, out_encoding="utf-8")
    rel2 = compare(dat, big)
    grow_ok = bool(rel2.relocations) and not rel2.suspects and not rel2.structural
    log(f"[27] compare(original, crescido): {len(rel2.relocations)} relocacoes esperadas, "
        f"{len(rel2.suspects)} suspeitos ({'OK' if grow_ok else 'FALHOU'})")
    if rel2.suspects:
        for w in rel2.suspects[:5]:
            log(f"     ! 0x{w.off:06X} {w.where}: 0x{w.old:08X} -> 0x{w.new:08X}")
    ok &= grow_ok

    # sabotagem: um imediato reescrito sem alvo logico tem que virar SUSPEITO
    dados = bytearray(big.read_bytes())
    sabotado = parse(bytes(dados))
    alvo = next(e for e in sabotado.elements if e.kind == "action" and e.params)
    pos = alvo.offset + 16 + 8            # param 0, word 2
    struct.pack_into("<I", dados, pos, 0x1234)
    ruim = tmpdir / "out" / "fit_sabotado.DAT"
    ruim.write_bytes(bytes(dados))
    rel3 = compare(dat, ruim)
    sab_ok = any(w.off == pos and not w.expected for w in rel3.words)
    log(f"[28] compare acha o imediato sabotado em 0x{pos:X}: "
        f"{'OK' if sab_ok else 'FALHOU'} ({len(rel3.suspects)} suspeitos)")
    ok &= sab_ok

    # -- fit no layout "classic", que grava o tamanho util e so aceita ate 4
    #    bytes de padding: ali a sobra vira espaco antes do NUL final
    cdat = tmpdir / "fit_classic.DAT"
    cdat.write_bytes(make_sample(["Um texto original suficientemente longo.", "Curto"], "utf-8"))
    cscript = parse(cdat.read_bytes())
    centries = collect_entries(cscript, "utf-8")
    for e in centries:
        e.translation = "Cabe" if classify_text(e.original) != "id" else e.original
    ctexts = tmpdir / "fit_classic.json"
    dump_entries(centries, ctexts, cdat.name, "utf-8", "json")
    cout = tmpdir / "out" / "fit_classic.DAT"
    crep = inject_file(cdat, ctexts, cout, out_encoding="utf-8", fit=True)
    cnovo = parse(cout.read_bytes())
    ctextos = [e.original.rstrip() for e in collect_entries(cnovo, "utf-8")]
    classic_ok = (len(cout.read_bytes()) == len(cdat.read_bytes())
                  and crep.layout_preserved and crep.overflow == 0
                  and ctextos[0] == "Cabe")
    log(f"[29] fit no layout classic (sobra vira espaco): "
        f"{'OK' if classic_ok else 'FALHOU'} - {ctextos}")
    ok &= classic_ok

    # -- .DAT em cp932 gravado em utf-8: ler o original com a codificacao de
    #    SAIDA fazia o texto "mudar" sem ter mudado e disparava o aviso de
    #    divergencia aos milhares. Ler e gravar sao codificacoes diferentes.
    # '\u51dc\u3000' em cp932 sao os bytes EA A3 81 40, que TAMBEM sao utf-8
    # valido e leem como '\ua8c1@'. E o caso real: o bloco decodifica nas duas
    # codificacoes, com resultados diferentes, entao ler com a errada nao falha
    # - so mente.
    ambiguo = "\u51dc\u3000"
    assert ambiguo.encode("cp932").decode("utf-8") != ambiguo
    ddat = tmpdir / "enc.DAT"
    ddat.write_bytes(make_otome_sample([ambiguo] + inline, pool))
    dscript = parse(ddat.read_bytes())
    dentries = collect_entries(dscript, "cp932")
    for e in dentries:
        e.translation = "" if classify_text(e.original) == "id" else "Tudo bem por aqui."
    dtexts = tmpdir / "enc.json"
    dump_entries(dentries, dtexts, ddat.name, "cp932", "json")
    dout = tmpdir / "out" / "enc.DAT"
    drep = inject_file(ddat, dtexts, dout, out_encoding="utf-8")
    falsos = [m for m in drep.problems if "divergente" in m]
    enc_ok = not falsos
    log(f"[30] cp932 lido como cp932 mesmo gravando utf-8 "
        f"(0 divergencias falsas): {'OK' if enc_ok else 'FALHOU'}")
    for m in falsos[:3]:
        log(f"     ! {m}")
    ok &= enc_ok

    # -- o meta do .json MENTINDO sobre a codificacao do .DAT. Acontece quando
    #    o extract detecta torto: sem --out-encoding a leitura vinha do meta, e
    #    o arquivo inteiro virava "texto original divergente".
    mdump = tmpdir / "enc_meta.json"
    dump_entries(dentries, mdump, ddat.name, "utf-8", "json")   # meta MENTE: e cp932
    mout = tmpdir / "out" / "enc_meta.DAT"
    mrep = inject_file(ddat, mdump, mout, fallback="ascii")     # sem --out-encoding
    mfalsos = [m for m in mrep.problems if "divergente" in m]
    avisou = any("diz que o .DAT esta em" in m for m in mrep.problems)
    acertos, testados = mrep.match_originais
    meta_ok = (mrep.src_encoding == "cp932" and not mfalsos and avisou
               and acertos == testados and testados > 0)
    log(f"[31] meta do .json mente a codificacao: lido como {mrep.src_encoding}, "
        f"{acertos}/{testados} textos casam, aviso={avisou}, "
        f"{len(mfalsos)} divergencias falsas ({'OK' if meta_ok else 'FALHOU'})")
    for m in mrep.problems[:3]:
        log(f"     - {m}")
    ok &= meta_ok

    # -- latin-1/cp1252 decodificam QUALQUER byte, entao vencem toda disputa
    #    decidida por "quantos blocos decodificam". Bastam alguns blocos nao
    #    textuais para o cp932 perder e o roteiro japones inteiro sair mojibake
    #    no .json ('\u767a\u8a00\u8005\u540d' virava '\u201d\xad\u0152\xbe\u017d\xd2\u2013\xbc').
    #    (os bytes FD FE FF nao existem em cp932 e sao letras em latin-1, entao
    #    entram no arquivo por substituicao crua, do mesmo tamanho)
    #    o japones da amostra precisa ser do tipo que o cp1252 aceita inteiro
    #    (sem os bytes 81/8D/8F/90/9D, que ele recusa) - senao o cp1252 ja perde
    #    sozinho e o teste nao prova nada. '\u767a\u8a00\u8005\u540d' e exatamente esse caso.
    jp_ambiguo = ["\u767a\u8a00\u8005\u540d", "\u4f1a\u8a71", "\u9078\u629e\u80a2", "\u80cc\u666f", "\u97f3\u697d", "\u6642\u9593", "\u540d\u524d"]
    pool_ambiguo = ["\u4f1a\u8a71\u9078\u629e", "\u80cc\u666f\u97f3\u697d"]
    bruto = bytearray(make_otome_sample(jp_ambiguo + ["aaaa", "bbbb"], pool_ambiguo))
    bruto = bruto.replace(b"aaaa", b"\xfd\xfe\xff\xfd").replace(b"bbbb", b"\xfe\xff\xfd\xfe")
    sujo = tmpdir / "latin.DAT"
    sujo.write_bytes(bytes(bruto))
    escolhida = detect_encoding(parse(sujo.read_bytes()))
    lat_ok = escolhida == "cp932"
    log(f"[32] cp932 nao perde para latin-1 por causa de bloco nao textual: "
        f"escolheu {escolhida} ({'OK' if lat_ok else 'FALHOU'})")
    ok &= lat_ok

    # -- 'original' descrevendo outro bloco. Escrever assim troca uma string
    #    pela traducao de OUTRA - foi como 'switch' virou 'trocar'.
    sdat = tmpdir / "switch.DAT"
    sdat.write_bytes(make_otome_sample(["switch"] + inline, pool))
    sscript = parse(sdat.read_bytes())
    sentries = collect_entries(sscript, "cp932")
    alvo_sw = next(e for e in sentries if e.original == "switch")
    antes = sscript.elements[alvo_sw.elem].segments[alvo_sw.seg].content
    alvo_sw.original = "trocar"          # .json contaminado
    alvo_sw.translation = "mudar"
    stexts = tmpdir / "switch.json"
    dump_entries(sentries, stexts, sdat.name, "cp932", "json")
    sout = tmpdir / "out" / "switch.DAT"
    srep = inject_file(sdat, stexts, sout, out_encoding="utf-8")
    depois = parse(sout.read_bytes()).elements[alvo_sw.elem].segments[alvo_sw.seg].content
    div_ok = (srep.skip_divergente == 1 and depois == antes == b"switch")
    log(f"[33] entrada divergente NAO e injetada: bloco continua {depois!r}, "
        f"{srep.skip_divergente} pulada(s) por divergencia ({'OK' if div_ok else 'FALHOU'})")
    ok &= div_ok

    # defesa em camadas: 'trocar' e token unico sem pontuacao, entao o guard de
    # identificador segura mesmo com --ignore-mismatch. So os DOIS juntos forcam.
    sout2 = tmpdir / "out" / "switch_meio.DAT"
    inject_file(sdat, stexts, sout2, out_encoding="utf-8", ignore_mismatch=True)
    meio = parse(sout2.read_bytes()).elements[alvo_sw.elem].segments[alvo_sw.seg].content
    sout3 = tmpdir / "out" / "switch_forcado.DAT"
    inject_file(sdat, stexts, sout3, out_encoding="utf-8",
                ignore_mismatch=True, allow_id_change=True)
    forcado = parse(sout3.read_bytes()).elements[alvo_sw.elem].segments[alvo_sw.seg].content
    forca_ok = meio == b"switch" and forcado == b"mudar"
    log(f"[34] --ignore-mismatch sozinho ainda segura ({meio!r}); com "
        f"--allow-id-change forca ({forcado!r}) ({'OK' if forca_ok else 'FALHOU'})")
    ok &= forca_ok

    # palavra-chave solta da engine nao pode ir para o tradutor num script ingles
    casos = {"switch": "id", "flag": "id", "jump": "id", "r": "id",
             "Yes.": "prose", "Ouch!": "prose", "Hello there": "prose",
             "bgm_theme_01.at9": "id", "NO00_0012": "id"}
    erros = {t: classify_text(t) for t, esperado in casos.items()
             if classify_text(t) != esperado}
    kw_ok = not erros
    log(f"[35] palavra-chave solta ('switch', 'flag', 'r') classificada como "
        f"identificador: {'OK' if kw_ok else 'FALHOU'}")
    if erros:
        log(f"     {erros}")
    ok &= kw_ok

    # -- o ponto cego do compare: um imediato do jogo que POR ACASO vale um
    #    endereco conhecido e relocado junto, e o mapeamento dele bate igual ao
    #    de um ponteiro legitimo - "relocacao esperada", zero suspeitos. So a
    #    consistencia por slot denuncia: ponteiro de verdade e relocado em quase
    #    toda instancia do opcode; coincidencia aparece numa so.
    cdat = tmpdir / "coincidencia.DAT"
    # muitas falas para o opcode repetir: a razao do slot so denuncia a
    # coincidencia se houver instancias suficientes para comparar
    muitas = [f"\u53f0\u8a5e{i}\u3067\u3059" for i in range(21)]
    base = parse(make_otome_sample(muitas, pool))
    acoes = [e for e in base.elements if e.kind == "action" and e.params]
    # o endereco tem que ser de algo que SE MOVE quando o texto cresce - o pool
    # da cauda serve; um elemento do inicio do arquivo ficaria parado e o
    # imediato nem seria reescrito
    endereco = base.elements[-1].offset
    vitima = acoes[-1]
    vitima.params[0][2] = endereco             # imediato que "parece" ponteiro
    cdat.write_bytes(build(base))

    cscript = parse(cdat.read_bytes())
    centries = collect_entries(cscript, "cp932")
    for e in centries:
        if classify_text(e.original) != "id":
            e.translation = f"Traducao {e.id} bem maior que o original japones."
    ctexts = tmpdir / "coincidencia.json"
    dump_entries(centries, ctexts, cdat.name, "cp932", "json")
    cout = tmpdir / "out" / "coincidencia.DAT"
    inject_file(cdat, ctexts, cout, out_encoding="utf-8")

    rel4 = compare(cdat, cout)
    pego = [t for t in rel4.isolados if t.wi == 2]
    coin_ok = bool(pego) and not rel4.suspects
    log(f"[36] compare denuncia o imediato coincidente: {len(rel4.suspects)} suspeitos "
        f"(o mapeamento dele bate), {len(rel4.isolados)} slot(s) isolado(s) "
        f"({'OK' if coin_ok else 'FALHOU'})")
    for t in rel4.isolados[:3]:
        log(f"     opcode 0x{t.opcode:X} param {t.pi} word {t.wi}: "
            f"{t.relocados}/{t.instancias} ({t.razao:.0%})")
    ok &= coin_ok

    # -- o teste que separa ponteiro de acaso, e o modo --relocate slots -------
    # param 0 word 0 e ponteiro de verdade (aponta sempre). param 0 word 2 vira
    # um imediato do jogo: numeros 4-alinhados dentro da faixa de enderecos, dos
    # quais so uma fracao cai sobre um endereco - que e como o acaso se parece.
    sdat2 = tmpdir / "slots.DAT"
    # amostra maior: o veredito "acaso" exige acertos suficientes para nao ser
    # ruido - com poucas instancias a resposta honesta e "duvidoso"
    mais = [f"\u53f0\u8a5e{i}\u3067\u3059\u3088" for i in range(60)]
    base2 = parse(make_otome_sample(mais, pool))
    acoes2 = [e for e in base2.elements if e.kind == "action" and e.params]
    for i, el in enumerate(acoes2):
        el.params[0][2] = 0x40 + i * 4        # imediato plausivel, nao escolhido
    sdat2.write_bytes(build(base2))

    sc2 = parse(sdat2.read_bytes())
    vereditos = slot_verdicts(sc2)
    ponteiro = [v for v in vereditos.values() if v.wi == 0 and v.veredito == "ponteiro"]
    acaso = [v for v in vereditos.values() if v.wi == 2 and v.veredito == "acaso"]
    nao_ponteiro = [v for v in vereditos.values()
                    if v.wi == 2 and v.candidatas and v.veredito != "ponteiro"]
    sep_ok = bool(ponteiro) and bool(acaso) and len(nao_ponteiro) == 3
    log(f"[37] slot_verdicts separa ponteiro de acaso: {len(ponteiro)} ponteiro(s), "
        f"{len(acaso)} acaso ({'OK' if sep_ok else 'FALHOU'})")
    for v in list(vereditos.values()):
        if v.candidatas:
            log(f"     0x{v.opcode:X} p{v.pi} w{v.wi}: {v.acertos}/{v.candidatas} "
                f"candidatas = {v.precisao:.0%} (densidade {v.densidade:.0%}) -> {v.veredito}")
    ok &= sep_ok

    ent2 = collect_entries(sc2, "cp932")
    for e in ent2:
        if classify_text(e.original) != "id":
            e.translation = f"Traducao {e.id} bem maior que o original japones."
    j2 = tmpdir / "slots.json"
    dump_entries(ent2, j2, sdat2.name, "cp932", "json")

    o_scan = tmpdir / "out" / "slots_scan.DAT"
    o_slot = tmpdir / "out" / "slots_slots.DAT"
    inject_file(sdat2, j2, o_scan, out_encoding="utf-8", relocate="scan")
    inject_file(sdat2, j2, o_slot, out_encoding="utf-8", relocate="slots")

    def _w2(caminho: Path) -> list[int]:
        sc = parse(caminho.read_bytes())
        return [e.params[0][2] for e in sc.elements if e.kind == "action" and e.params]

    antes_w2 = [e.params[0][2] for e in acoes2]
    scan_mexeu = _w2(o_scan) != antes_w2
    slots_preservou = _w2(o_slot) == antes_w2
    # e o ponteiro de verdade continua sendo seguido nos DOIS modos
    r_slots = compare(sdat2, o_slot)
    ponteiro_ok = bool(r_slots.relocations) and not r_slots.suspects
    modo_ok = scan_mexeu and slots_preservou and ponteiro_ok
    log(f"[38] --relocate slots: scan mexeu no imediato={scan_mexeu}, "
        f"slots preservou={slots_preservou}, ponteiro ainda relocado="
        f"{ponteiro_ok} ({'OK' if modo_ok else 'FALHOU'})")
    ok &= modo_ok

    # -- o que sobrou sem traduzir, e o motivo de cada caso -------------------
    pdat = tmpdir / "pend.DAT"
    pdat.write_bytes(make_otome_sample(inline, pool))
    psc = parse(pdat.read_bytes())
    pent = collect_entries(psc, "cp932")
    falas = [e for e in pent if classify_text(e.original) == "cjk"]
    falas[0].translation = "Traduzida de verdade."
    falas[1].translation = ""                       # sem traducao
    falas[2].translation = falas[2].original        # tradutor devolveu igual
    pj = tmpdir / "pend.json"
    dump_entries(pent, pj, pdat.name, "cp932", "json")
    pout = tmpdir / "out" / "pend.DAT"
    inject_file(pdat, pj, pout, out_encoding="utf-8")

    pend, _ = pendencias(pout, pj)
    vistos = {q.entry_id: q.motivo for q in pend}
    motivos_vistos = set(vistos.values())
    pend_ok = ("sem traducao" in motivos_vistos
               and "traducao igual ao original" in motivos_vistos
               # a que foi traduzida de verdade NAO pode aparecer como pendencia
               and falas[0].id not in vistos)
    log(f"[39] pending explica cada pendencia: {'OK' if pend_ok else 'FALHOU'}")
    for q in pend[:4]:
        log(f"     [{q.entry_id}] ({q.kind}) <- {q.motivo}")
    ok &= pend_ok
    return bool(ok)


def _check_shorten(tmpdir: Path, log) -> bool:
    """Orcamento por fala (largura do original) e a escalada de encurtamento."""
    from .shorten import candidatas_orcadas, shorten_entries
    from .textio import (
        box_budget, box_overflow, display_width, entry_budget, line_count,
        piso_do_lote,
    )
    ok = True

    # -- [40] primitivas de linha --------------------------------------------
    tres = "Linha um aqui\nLinha dois aqui\nLinha tres aqui"
    cinco = "\n".join(f"Linha {i}" for i in range(5))
    literal = "Linha um\\nLinha dois\\nLinha tres"
    marcada = "#Name[2]" * 12 + "curto"
    kana = " ".join("\u3042" * 10 for _ in range(6))
    conta_ok = (line_count(tres) == 3 and line_count(cinco) == 5
                and line_count(literal) == 3 and line_count("") == 0)
    box_ok = (box_overflow(tres, 50, 3) == 0
              and box_overflow(cinco, 50, 3) == 2
              and box_overflow(literal, 50, 3) == 0
              and box_overflow(marcada, 50, 3) == 0
              and line_count(wrap_text(kana, 50)) == 3
              # japones corrido nao tem ponto de quebra: fica numa linha so
              and line_count(wrap_text("\u3042" * 40, 50)) == 1)
    log(f"[40] primitivas de linha (marcador, kana, japones corrido): "
        f"{'OK' if conta_ok and box_ok else 'FALHOU'}")
    ok &= conta_ok and box_ok

    # -- [40b] primitivas de largura e orcamento ------------------------------
    largura_ok = (display_width("#Name[2]abc") == 3        # marcador nao ocupa
                  and display_width("ab\ncd") == 4          # quebra real nao ocupa
                  and display_width("ab\\ncd") == 4         # nem a literal
                  and display_width("\u3042\u3044") == 4       # kana = 2 colunas
                  and display_width("") == 0)
    # RE-quebrar nao pode mudar a medida - e disso que depende nao reclassificar
    # uma fala ja encurtada como candidata. (A primeira quebra muda mesmo: o
    # wrap_text descarta o espaco do ponto de quebra, de proposito.)
    _q = wrap_text(kana, 50)
    idem_ok = display_width(wrap_text(_q, 50)) == display_width(_q)
    piso_ok = (piso_do_lote(["a" * 40] * 9 + ["a" * 160]) == 40   # exclui o outlier
               and piso_do_lote([]) == 0)
    # ⚠ identificador e curto e numeroso - num script tipico e a MAIORIA das
    # entradas. Se entrar na populacao do percentil, afunda o piso e aperta o
    # orcamento de todo mundo.
    mistura = ([TextEntry(id=f"A{i:05d}_S00", elem=i, seg=0, opcode=None,
                          encoding="cp932", original="NO00_0012", translation="x")
                for i in range(90)]
               + [TextEntry(id=f"B{i:05d}_S00", elem=i, seg=0, opcode=None,
                            encoding="cp932", original="\u3042" * 30, translation="x")
                  for i in range(10)])
    from .textio import originais_de_fala
    populacao_ok = (len(originais_de_fala(mistura)) == 10
                    and piso_do_lote(originais_de_fala(mistura)) == 60
                    # sem o filtro, os 90 identificadores afundariam o piso
                    and piso_do_lote([e.original for e in mistura]) < 60)
    teto = box_budget(50, 3)
    orc_ok = (entry_budget("\u3048\uff1f", piso=40) == 40             # o piso segura
              and entry_budget("\u3042" * 20, piso=40) == 46        # 40 col +15%
              and entry_budget("\u3042" * 80, piso=40, teto=teto) == teto   # o teto trava
              and entry_budget("", piso=0) == 0                # sem base
              and entry_budget("a" * 40, folga=0.30) >= entry_budget("a" * 40, folga=0.15))
    e40b = largura_ok and idem_ok and piso_ok and populacao_ok and orc_ok
    log(f"[40b] largura e orcamento (marcador, quebra, piso sem identificador, teto): "
        f"{'OK' if e40b else 'FALHOU'}")
    if not e40b:
        log(f"     largura={largura_ok} idempotente={idem_ok} piso={piso_ok} "
            f"populacao={populacao_ok} orc={orc_ok}")
    ok &= e40b

    # -- montagem comum aos testes de escalada --------------------------------
    def _e(i, jp, pt):
        return TextEntry(id=TextEntry.make_id(i, 0), elem=i, seg=0, opcode=None,
                         encoding="cp932", original=jp, translation=pt)

    JP_CURTO, JP_MEDIO, JP_GIGANTE = "\u3048\uff1f", "\u3042" * 20, "\u3042" * 80
    longa = ("Uma fala propositalmente enorme que nao cabe de jeito nenhum na caixa "
             "do jogo porque tem muito mais texto do que o original comportava, e "
             "segue por mais um tanto so para garantir o estouro de largura.")

    def _com_recheio(alvos):
        """Enche o lote para o P90 ser o 40 do meio, e nao o outlier de 160."""
        recheio = [_e(100 + i, JP_MEDIO, "Curta.") for i in range(19)]
        return list(alvos) + recheio

    # -- [41] a escalada, e o orcamento que chega ao modelo -------------------
    ents = _com_recheio([
        _e(1, JP_CURTO, longa),        # piso
        _e(2, JP_MEDIO, longa),        # 40 col +15%
        _e(3, JP_GIGANTE, longa * 3),  # teto
        _e(4, "SYSTEM_GLOBAL", longa), # identificador: nunca entra
    ])
    esperados = {1: 40, 2: 46, 3: teto}
    alvos = candidatas_orcadas(ents, 50, 3)
    sel_ok = {e.id for e, _ in alvos} == {ents[0].id, ents[1].id, ents[2].id}
    orc_por_id = {e.id: orc for e, orc in alvos}
    certo_ok = all(orc_por_id.get(TextEntry.make_id(i, 0)) == v
                   for i, v in esperados.items())

    chamadas: list[tuple[bool, list[str], list[int]]] = []

    def falso(textos, orcamentos, resumir):
        chamadas.append((resumir, list(textos), list(orcamentos)))
        return ["Curta." if resumir else t for t in textos]

    rep = shorten_entries(ents, falso, max_line=50, max_lines=3, log=lambda m: None)
    # ⚠ ESTA e a asserção que reprova um `[orcamento] * len(lote)`
    variados_ok = bool(chamadas) and len(set(chamadas[0][2])) > 1
    duas_passadas = len(chamadas) == 2 and chamadas[1][0] is True
    mesmo_orc = (duas_passadas
                 and all(o in chamadas[0][2] for o in chamadas[1][2]))
    intocado = ents[3].translation == longa
    e41 = sel_ok and certo_ok and variados_ok and duas_passadas and mesmo_orc and intocado
    log(f"[41] orcamento POR FALA chega ao modelo "
        f"(piso {orc_por_id.get('A00001_S00')}, +15% {orc_por_id.get('A00002_S00')}, "
        f"teto {orc_por_id.get('A00003_S00')}): {'OK' if e41 else 'FALHOU'}")
    if not e41:
        log(f"     selecao={sel_ok} valores={certo_ok} variados={variados_ok} "
            f"passadas={[(r, o) for r, _, o in chamadas]} mesmo_orc={mesmo_orc} "
            f"id_intocado={intocado}")
    ok &= e41

    # -- [41b] dedup pelo PAR (texto, orcamento) ------------------------------
    mesmos = _com_recheio([_e(1, JP_CURTO, longa), _e(2, JP_MEDIO, longa)])
    ch2: list[list[int]] = []

    def falso2(textos, orcamentos, resumir):
        ch2.append(list(orcamentos))
        return ["Curta." for _ in textos]

    shorten_entries(mesmos, falso2, max_line=50, max_lines=3, log=lambda m: None)
    # a MESMA traducao com orcamentos diferentes vai duas vezes
    par_ok = bool(ch2) and len(ch2[0]) == 2 and len(set(ch2[0])) == 2

    iguais = _com_recheio([_e(1, JP_MEDIO, longa), _e(2, JP_MEDIO, longa)])
    ch3: list[list[int]] = []

    def falso3(textos, orcamentos, resumir):
        ch3.append(list(orcamentos))
        return ["Curta." for _ in textos]

    shorten_entries(iguais, falso3, max_line=50, max_lines=3, log=lambda m: None)
    # mesmo texto E mesmo orcamento: o dedup continua valendo
    dedup_ok = bool(ch3) and len(ch3[0]) == 1
    e41b = par_ok and dedup_ok
    log(f"[41b] dedup pelo par: orcamentos diferentes vao separados "
        f"({ch2[0] if ch2 else '?'}), iguais colapsam ({ch3[0] if ch3 else '?'}): "
        f"{'OK' if e41b else 'FALHOU'}")
    ok &= e41b

    # -- [41c] o cache nao vaza entre orcamentos ------------------------------
    cache = tmpdir / "shorten-cache.json"
    contador = {"n": 0}

    def falso4(textos, orcamentos, resumir):
        contador["n"] += len(textos)
        return ["Curta." for _ in textos]

    a = _com_recheio([_e(1, JP_MEDIO, longa)])
    shorten_entries(a, falso4, max_line=50, max_lines=3, cache_path=cache,
                    log=lambda m: None)
    apos_1 = contador["n"]
    b = _com_recheio([_e(1, JP_MEDIO, longa)])
    r2 = shorten_entries(b, falso4, max_line=50, max_lines=3, cache_path=cache,
                         log=lambda m: None)
    apos_2 = contador["n"]
    c = _com_recheio([_e(1, JP_MEDIO, longa)])
    shorten_entries(c, falso4, max_line=50, max_lines=3, folga=0.40,
                    cache_path=cache, log=lambda m: None)
    apos_3 = contador["n"]
    e41c = apos_1 > 0 and apos_2 == apos_1 and r2.do_cache >= 1 and apos_3 > apos_2
    log(f"[41c] cache: 1a rodada {apos_1} chamadas, repetida {apos_2} (reaproveitou), "
        f"com outra folga {apos_3} (chamou de novo): {'OK' if e41c else 'FALHOU'}")
    ok &= e41c

    # -- [41d] escalada disparada por LARGURA, nao por linha ------------------
    # 60 colunas cabem em 2 linhas de 50, entao box_overflow == 0; mas passam do
    # orcamento de 46 do original. Com o _cabe antigo isto nunca escalaria.
    media = "Uma fala de tamanho medio que cabe em duas linhas mas passa da largura."
    largura_ents = _com_recheio([_e(1, JP_MEDIO, media)])
    sem_linhas = box_overflow(media, 50, 3) == 0
    entrou = any(e.id == "A00001_S00" for e, _ in candidatas_orcadas(largura_ents, 50, 3))
    ch5: list[bool] = []

    def falso5(textos, orcamentos, resumir):
        ch5.append(resumir)
        return ["Curta." if resumir else t for t in textos]

    shorten_entries(largura_ents, falso5, max_line=50, max_lines=3, log=lambda m: None)
    e41d = sem_linhas and entrou and ch5 == [False, True]
    log(f"[41d] fala que cabe nas linhas mas estoura a largura escala para o resumo: "
        f"{'OK' if e41d else 'FALHOU'}")
    if not e41d:
        log(f"     cabe_em_linhas={sem_linhas} virou_candidata={entrou} passadas={ch5}")
    ok &= e41d

    # -- [42] marcadores, deficit em colunas, e a guarda de nao-regressao -----
    com_tag = "#Name[2]" + longa
    ents2 = _com_recheio([_e(1, JP_MEDIO, com_tag)])

    def perde_tudo(textos, orcamentos, resumir):
        return [longa for _ in textos]      # sem os placeholders E ainda largo

    rep2 = shorten_entries(ents2, perde_tudo, max_line=50, max_lines=3,
                           log=lambda m: None)
    e = ents2[0]
    marcador_ok = (e.needs_review and rep2.restantes == 1
                   and "#Name[2]" in e.translation
                   and any("faltam" in n and "coluna" in n for n in e.notes))

    # a guarda antiga (so linhas) jogaria fora uma reescrita que corta colunas
    # mas mantem o numero de linhas. A nova tem que ACEITAR.
    # 92 colunas -> 2 linhas; 56 colunas -> tambem 2 linhas. Mesmo numero de
    # linhas, largura bem menor: e o caso que a guarda antiga descartava.
    duas_linhas = ("Uma fala com bastante texto aqui que ocupa exatamente duas "
                   "linhas inteiras na caixa do jogo.")
    menor = "Uma fala bem menor que ainda ocupa duas linhas na caixa."
    ents3 = _com_recheio([_e(1, JP_MEDIO, duas_linhas)])
    shorten_entries(ents3, lambda t, o, r: [menor for _ in t],
                    max_line=50, max_lines=3, log=lambda m: None)
    aceitou = ents3[0].translation.replace("\n", " ") == menor
    e42 = marcador_ok and aceitou
    log(f"[42] marcador perdido vira needs_review com deficit em colunas, e a "
        f"guarda aceita corte de largura: {'OK' if e42 else 'FALHOU'}")
    if not e42:
        log(f"     marcador={marcador_ok} aceitou_corte={aceitou} "
            f"notes={ents2[0].notes} depois={ents3[0].translation!r}")
    ok &= e42
    return bool(ok)


def run(verbose: bool = True) -> bool:
    log = print if verbose else (lambda *a, **k: None)
    ok = True
    samples = {
        "utf8": ("utf-8", [
            "#Name[2]「Bom dia.」#KW_F[]",
            "Linha um\nLinha dois #KW_ED[]",
            "Texto curto",
            "Um texto consideravelmente mais longo para testar o crescimento do bloco.",
        ]),
        "cp932": ("cp932", [
            "#Name[2]「おはようございます。」#KW_F[]",
            "ここはテストです。\n二行目 #KW_ED[]",
            "短い",
        ]),
    }

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for label, (enc, texts) in samples.items():
            log(f"\n=== amostra {label} ({enc}) ===")
            dat = tmpdir / f"{label}.DAT"
            dat.write_bytes(make_sample(texts, enc))

            # 1) round-trip binario
            same, detail = roundtrip_check(dat.read_bytes())
            log(f"[1] round-trip byte-a-byte: {'OK' if same else 'FALHOU'} ({detail})")
            ok &= same

            # 2) deteccao de encoding + extracao
            script = parse(dat.read_bytes())
            found = detect_encoding(script)
            entries = collect_entries(script, found)
            expected = texts + ["SYSTEM_GLOBAL"]
            got = [e.original for e in entries]
            enc_ok = found == enc or (enc == "utf-8" and found in ("utf-8", "ascii"))
            text_ok = got == expected
            log(f"[2] encoding detectado: {found} ({'OK' if enc_ok else 'DIVERGENTE'})")
            log(f"[3] textos extraidos: {len(entries)} ({'OK' if text_ok else 'FALHOU'})")
            if not text_ok:
                log(f"    esperado={expected}\n    obtido  ={got}")
            ok &= enc_ok and text_ok

            # 3) traducao simulada: PT-BR com acentos, tamanhos diferentes
            for i, e in enumerate(entries):
                e.translation = ("Tradução número %d — com acentuação, çedilha e um "
                                 "tamanho bem diferente do original." % i) if i % 2 == 0 else "Curto"
                # marcadores preservados manualmente (como faria o tradutor)
                for tag in ("#Name[2]", "#KW_F[]", "#KW_ED[]"):
                    if tag in e.original:
                        e.translation = tag + e.translation
            texts_file = tmpdir / f"{label}.json"
            dump_entries(entries, texts_file, dat.name, "utf-8", "json")

            # 4) injecao (saida sempre UTF-8: cp932 nao possui acentos latinos)
            out_dat = tmpdir / "out" / f"{label}.DAT"
            report = inject_file(dat, texts_file, out_dat, out_encoding="utf-8")
            # identificador (SYSTEM_GLOBAL) tem a traducao recusada de proposito
            ids = [e for e in entries if classify_text(e.original) == "id"]
            log(f"[4] injecao: {report.applied} aplicados, {report.skipped} pulados, "
                f"{report.grown} blocos maiores que o original")
            for p in report.problems:
                log(f"    ! {p}")
            inj_ok = (report.applied == len(entries) - len(ids)
                      and report.id_changes == len(ids)
                      and not any(p.startswith("ERRO") for p in report.problems))
            log(f"    identificadores preservados: {report.id_changes}/{len(ids)} "
                f"({'OK' if inj_ok else 'FALHOU'})")
            ok &= inj_ok

            # 5) reabre e confere textos + ponteiros
            new = parse(out_dat.read_bytes())
            new_entries = collect_entries(new, "utf-8")
            # o inject quebra as falas em 50 colunas por padrao
            want = [e.original if classify_text(e.original) == "id"
                    else wrap_text(e.translation) for e in entries]
            got = [e.original for e in new_entries]
            text_ok = got == want
            log(f"[5] textos apos injecao (ja quebrados em 50): "
                f"{'OK' if text_ok else 'FALHOU'}")
            if not text_ok:
                log(f"    esperado={want}\n    obtido  ={got}")
            ok &= text_ok

            ptr_ok = True
            for el in new.elements:
                if el.kind != "action" or not el.params:
                    continue
                expected = el.offset + el.header_size()
                if el.params[0][0] != expected:
                    ptr_ok = False
                    log(f"    ! acao em 0x{el.offset:X}: ponteiro 0x{el.params[0][0]:X} "
                        f"deveria ser 0x{expected:X}")
            log(f"[6] ponteiros dos parametros: {'OK' if ptr_ok else 'FALHOU'}")
            ok &= ptr_ok

            hdr = new.header
            first_action = next(e for e in new.elements if e.kind == "action")
            exp_ok = bool(new.exports) and new.exports[0].offset == first_action.offset
            link_ok = hdr.collection_link in {e.offset for e in new.elements}
            tbl_ok = hdr.export_offset == align4(
                new.elements[-1].offset + new.elements[-1].size())
            log(f"[7] tabela de exports: {'OK' if exp_ok else 'FALHOU'} | "
                f"collection_link: {'OK' if link_ok else 'FALHOU'} | "
                f"export_offset: {'OK' if tbl_ok else 'FALHOU'}")
            ok &= exp_ok and link_ok and tbl_ok

            # 6) o arquivo gerado tem que sobreviver a um novo round-trip
            same2, detail2 = roundtrip_check(out_dat.read_bytes())
            log(f"[8] round-trip do arquivo traduzido: {'OK' if same2 else 'FALHOU'} ({detail2})")
            ok &= same2

        # 7) formato TXT
        entries2, meta = load_entries(tmpdir / "utf8.json")
        txt = tmpdir / "utf8.txt"
        dump_entries(entries2, txt, "utf8.DAT", "utf-8", "txt")
        back, meta2 = load_entries(txt)
        txt_ok = ([e.id for e in back] == [e.id for e in entries2]
                  and [e.original for e in back] == [e.original for e in entries2]
                  and [e.translation for e in back] == [e.translation for e in entries2])
        log(f"\n[9] ida e volta no formato TXT (com \\n e acentos): "
            f"{'OK' if txt_ok else 'FALHOU'}")
        ok &= txt_ok

        # 8) protecao de marcadores
        from .textio import restore_tags
        src = "#Name[2]「テスト」#KW_F[]\n{item} %GOLD% \\n fim"
        prot, tags = protect_tags(src)
        rest, tag_ok = restore_tags(prot, tags)
        prot_ok = rest == src and tag_ok and len(tags) == 5
        log(f"[10] protecao/restauracao de marcadores: {'OK' if prot_ok else 'FALHOU'} "
            f"({len(tags)} marcadores)")
        ok &= prot_ok

        # 10) leitura da resposta do gtx em lote (as DUAS formas, sem rede)
        from .translate import LoteRecusado, TranslationError, _gtx_normaliza
        plana = _gtx_normaliza(["Ola", "Adeus"], 2)                  # sl=ja
        pares = _gtx_normaliza([["Ola", "ja"], ["Ola mundo", "en"]], 2)  # sl=auto
        desalinhado = False
        try:
            _gtx_normaliza(["Ola"], 2)
        except LoteRecusado:
            desalinhado = True
        lixo = False
        try:
            _gtx_normaliza({"error": "quota"}, 1)
        except TranslationError:
            lixo = True
        gtx_ok = (plana == ["Ola", "Adeus"] and pares == ["Ola", "Ola mundo"]
                  and desalinhado and lixo)
        log(f"[11] resposta do gtx em lote (lista plana, pares de sl=auto, "
            f"contagem divergente): {'OK' if gtx_ok else 'FALHOU'}")
        if not gtx_ok:
            log(f"     plana={plana} pares={pares} "
                f"desalinhado={desalinhado} lixo={lixo}")
        ok &= gtx_ok

        # 11) dobra para cp932: acento E pontuacao tipografica, sem sobrar '?'
        from .textio import encode_text
        frase = "Nao sei\u2026 \u2014 \u201cVoc\u00ea est\u00e1 bem?\u201d \u2014 disse \u00e0 beira."
        dobrada = encode_text(frase, "cp932", fallback="ascii").decode("cp932")
        cp932_ok = "?" not in dobrada.replace("bem?", "") and " - " in dobrada
        log(f"[12] dobra para cp932 (acento + travessao, sem virar '?'): "
            f"{'OK' if cp932_ok else 'FALHOU'}")
        if not cp932_ok:
            log(f"     {dobrada!r}")
        ok &= cp932_ok

        # 12) quebra de linha para caber na caixa de texto
        from .textio import NEWLINE_LITERAL, detect_newline, visible_width

        def _largura_max(texto: str, quebra: str = "\n") -> int:
            linhas = texto.split(quebra)
            return max(visible_width(protect_tags(l)[0]) for l in linhas)

        fala = ("#Name[2]Eu nao consigo acreditar que voce realmente veio ate aqui "
                "so para me ver, depois de tudo o que aconteceu ontem. #KW_ED[]")
        quebrada = wrap_text(fala, 50)
        largura_ok = _largura_max(quebrada) <= 50
        tags_ok = "#Name[2]" in quebrada and "#KW_ED[]" in quebrada
        palavras_ok = quebrada.split() == fala.split()      # nada perdido nem partido
        log(f"[13] quebra em 50 colunas (largura={_largura_max(quebrada)}, "
            f"marcadores inteiros, palavras intactas): "
            f"{'OK' if largura_ok and tags_ok and palavras_ok else 'FALHOU'}")
        if not (largura_ok and tags_ok and palavras_ok):
            log(f"     {quebrada!r}")
        ok &= largura_ok and tags_ok and palavras_ok

        preexistente = ("Curta.\nUma segunda linha bem mais comprida do que caberia "
                        "na caixa de texto do jogo.")
        pre = wrap_text(preexistente, 50)
        pre_ok = (pre.split("\n")[0] == "Curta." and _largura_max(pre) <= 50
                  and pre.count("\n") > preexistente.count("\n"))
        log(f"[14] quebras que ja existiam sao preservadas: "
            f"{'OK' if pre_ok else 'FALHOU'}")
        if not pre_ok:
            log(f"     {pre!r}")
        ok &= pre_ok

        ids = ["NO00_0012", "bgm_theme_01.at9", "SYSTEM_GLOBAL"]
        id_ok = all(wrap_text(i, 5) == i for i in ids)
        log(f"[15] identificadores da engine nunca sao quebrados: "
            f"{'OK' if id_ok else 'FALHOU'}")
        ok &= id_ok

        idem_ok = wrap_text(quebrada, 50) == quebrada
        lit = wrap_text(fala, 50, NEWLINE_LITERAL)
        lit_ok = (NEWLINE_LITERAL in lit and "\n" not in lit
                  and _largura_max(lit, NEWLINE_LITERAL) <= 50
                  and wrap_text(lit, 50, NEWLINE_LITERAL) == lit)
        log(f"[16] idempotencia e modo \\n literal: "
            f"{'OK' if idem_ok and lit_ok else 'FALHOU'}")
        if not (idem_ok and lit_ok):
            log(f"     idem={idem_ok} literal={lit!r}")
        ok &= idem_ok and lit_ok

        class _E:
            def __init__(self, original): self.original = original
        nl_ok = (detect_newline([_E("a\nb"), _E("c\nd")]) == "\n"
                 and detect_newline([_E("a\\nb"), _E("c\\nd")]) == NEWLINE_LITERAL
                 and detect_newline([_E("sem quebra")]) == "\n"
                 and detect_newline([_E("a\nb")], "literal") == NEWLINE_LITERAL)
        log(f"[17] deteccao da forma da quebra (lf / literal / vazio / forcada): "
            f"{'OK' if nl_ok else 'FALHOU'}")
        ok &= nl_ok

        # 9) formato Otomate (sem CODE_END_, bloco wordcount, pool na cauda)
        log("\n=== amostra otomate (cp932, sem CODE_END_) ===")
        ok &= _check_otome(tmpdir, log)

        # 10) modo --fit (layout intacto) e o comando compare
        log("\n=== modo --fit e compare ===")
        ok &= _check_fit(tmpdir, log)

        # 11) orcamento de caixa e encurtamento (sem rede: modelo injetado)
        log("\n=== caixa de texto e encurtamento ===")
        ok &= _check_shorten(tmpdir, log)

    log("\n" + ("TODOS OS TESTES PASSARAM" if ok else "HOUVE FALHAS - veja acima"))
    return bool(ok)
