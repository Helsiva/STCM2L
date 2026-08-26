#!/usr/bin/env python3
"""Ponto de entrada da ferramenta. Requer Python 3.10+."""
import sys
from pathlib import Path

if sys.version_info < (3, 10):
    sys.exit("Python 3.10 ou superior e necessario (encontrado: %s)"
             % ".".join(map(str, sys.version_info[:3])))

sys.path.insert(0, str(Path(__file__).resolve().parent))

from stcm2l.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
