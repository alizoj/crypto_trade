"""Точка входа `quant` (python -m quant ...).

Каждый модуль регистрирует свою группу команд функцией register(subparsers).
"""

from __future__ import annotations

import argparse
import importlib
import logging
import sys

MODULES = [
    "quant.data.cli",          # quant instruments ..., quant data ...
    "quant.data.us_stocks",    # quant us ... (фаза 8)
    "quant.research.cli",      # quant research ...
    "quant.backtest.cli",      # quant backtest ..., quant sanity ...
    "quant.optimize.cli",      # quant optimize ...
    "quant.calibration.cli",   # quant calibrate ...
    "quant.live_cli",          # quant live ..., quant api ..., quant bot ..., quant auth ...
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quant", description="HashHedge quant system")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="group", required=True)
    for name in MODULES:
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError as e:
            if e.name == name:
                continue
            raise
        mod.register(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # консоль Windows (cp1251)
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    rc = args.func(args)
    return int(rc or 0)


if __name__ == "__main__":
    sys.exit(main())
