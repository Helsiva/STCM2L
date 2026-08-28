"""
stcm2l-tool - kit de traducao para scripts STCM2L (Otomate / Rejet - PS Vita).

Uso como biblioteca:

    from stcm2l import parse, build, collect_entries, detect_encoding

    script = parse(Path("SCRIPT.DAT").read_bytes())
    enc = detect_encoding(script)
    for entry in collect_entries(script, enc):
        print(entry.id, entry.original)
"""

from .core import (
    BuildError, DataBlock, Element, Export, Header, ParseError, RawSegment,
    Script, Stcm2lError, build, parse, roundtrip_check,
)
from .pipeline import (
    collect_entries, extract_file, inject_file, inspect, iter_inputs, verify_file,
)
from .textio import (
    MAX_LINE_DEFAULT, TextEntry, detect_encoding, detect_newline, dump_entries,
    load_entries, protect_tags, restore_tags, visible_width, wrap_entries, wrap_text,
)

__version__ = "1.0.0"

__all__ = [
    "Stcm2lError", "ParseError", "BuildError",
    "Header", "Element", "Export", "DataBlock", "RawSegment", "Script",
    "parse", "build", "roundtrip_check",
    "inspect", "verify_file", "extract_file", "inject_file",
    "collect_entries", "iter_inputs",
    "TextEntry", "detect_encoding", "dump_entries", "load_entries",
    "protect_tags", "restore_tags",
    "wrap_text", "wrap_entries", "visible_width", "detect_newline",
    "MAX_LINE_DEFAULT", "__version__",
]
