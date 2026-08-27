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
    DATA_HEADER_SIZE, EXPORT_ENTRY_SIZE, EXPORT_NAME_SIZE, HEADER_SIZE, align4,
    build, parse, roundtrip_check,
)
from .pipeline import collect_entries, inject_file
from .textio import detect_encoding, dump_entries, load_entries


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
    log(f"[13] otome: round-trip byte-a-byte: {'OK' if same else 'FALHOU'} ({detail})")
    ok &= same

    script = parse(dat.read_bytes())
    entries = collect_entries(script, "cp932")
    got = [e.original for e in entries]
    text_ok = got == inline + pool
    log(f"[14] otome: {len(entries)} textos extraidos "
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
    inj_ok = report.applied == len(entries) and not any(
        p.startswith("ERRO") for p in report.problems)
    log(f"[15] otome: injecao: {report.applied} aplicados, "
        f"{report.grown} maiores ({'OK' if inj_ok else 'FALHOU'})")
    ok &= inj_ok

    new = parse(out_dat.read_bytes())
    got = [e.original for e in collect_entries(new, "utf-8")]
    want = [e.translation for e in entries]
    text_ok = got == want
    log(f"[16] otome: textos apos injecao: {'OK' if text_ok else 'FALHOU'}")
    ok &= text_ok

    # os ponteiros para o pool da cauda tem que seguir o pool que se moveu
    destinos = {}
    for ei, si, db in new.iter_data_blocks():
        el = new.elements[ei]
        destinos[el.offset + el.segment_rel(si)] = db.content.decode("utf-8")
    esperado = [e.translation for e in entries[len(inline):]]
    apontados = []
    for el in new.elements:
        if el.kind == "action" and el.params and el.params[0][0]:
            apontados.append(destinos.get(el.params[0][0]))
    ptr_ok = bool(apontados) and all(a in esperado for a in apontados)
    log(f"[17] otome: ponteiros para o pool da cauda: {'OK' if ptr_ok else 'FALHOU'}")
    if not ptr_ok:
        log(f"     apontados={apontados}\n     esperado={esperado}")
    ok &= ptr_ok
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
            log(f"[4] injecao: {report.applied} aplicados, {report.skipped} pulados, "
                f"{report.grown} blocos maiores que o original")
            for p in report.problems:
                log(f"    ! {p}")
            ok &= report.applied == len(entries) and not any(
                p.startswith("ERRO") for p in report.problems)

            # 5) reabre e confere textos + ponteiros
            new = parse(out_dat.read_bytes())
            new_entries = collect_entries(new, "utf-8")
            want = [e.translation for e in entries]
            got = [e.original for e in new_entries]
            text_ok = got == want
            log(f"[5] textos apos injecao: {'OK' if text_ok else 'FALHOU'}")
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
        from .textio import protect_tags, restore_tags
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

        # 9) formato Otomate (sem CODE_END_, bloco wordcount, pool na cauda)
        log("\n=== amostra otomate (cp932, sem CODE_END_) ===")
        ok &= _check_otome(tmpdir, log)

    log("\n" + ("TODOS OS TESTES PASSARAM" if ok else "HOUVE FALHAS - veja acima"))
    return bool(ok)
