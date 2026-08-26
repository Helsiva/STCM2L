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
    DATA_HEADER_SIZE, EXPORT_ENTRY_SIZE, HEADER_SIZE, align4, build, parse,
    roundtrip_check,
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

    log("\n" + ("TODOS OS TESTES PASSARAM" if ok else "HOUVE FALHAS - veja acima"))
    return bool(ok)
