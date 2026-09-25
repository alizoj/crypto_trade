"""Догрузка гэпов через REST (3.2.3, gap-detector из раздела 4).

По открытым записям реестра с причиной unknown / source_outage: REST `/fapi/v1/klines` (гэп last-баров)
и `/fapi/v1/markPriceKlines` (гэп mark и заодно mark под гэпом баров) → raw rest_* (version=2) →
пересборка norm → повторный детект. Закрытые гэпы помечаются filled.

Эвристические exchange_maintenance (одновременный гэп у ≥80% символов) тоже проверяются через REST,
пока не подтверждены: одновременная дыра бывает и дефектом публикации архива (например, пропущенный
день markPriceKlines у всех символов), а не простоем биржи.
Если биржа за период баров не отдаёт — пропуск на стороне биржи: запись получает
reason=exchange_maintenance с пометкой «REST: …» # ASSUMPTION. Синтетические бары не создаются.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from quant.data import quality
from quant.data.backfill import rest_klines_range
from quant.data.build import build_symbol
from quant.data.datasets import REST_KLINES, REST_MARK, data_root
from quant.data.rest import BinanceREST
from quant.db import session_scope
from quant.db.models import GapRecord
from quant.instruments import by_symbol
from quant.timeutil import now_utc

log = logging.getLogger(__name__)

FILL_REASONS = ("unknown", "source_outage")
VERIFY_REASONS = ("exchange_maintenance",)   # эвристика без подтверждения REST — тоже проверяем
CONFIRMED_REASON = "exchange_maintenance"   # ASSUMPTION: REST пуст ⇒ пропуск на стороне биржи


def _todo(symbols: list[str] | None, reasons: tuple[str, ...], verify_maintenance: bool,
          max_minutes: int) -> list[dict]:
    out = []
    for g in quality.load_registry(symbols, open_only=True):
        if g["minutes"] > max_minutes:
            continue
        if g["reason"] in reasons:
            out.append(g)
        elif verify_maintenance and g["reason"] in VERIFY_REASONS and "REST:" not in (g["detail"] or ""):
            out.append(g)
    return out


def fill_gaps(symbols: list[str] | None = None, *, reasons: tuple[str, ...] = FILL_REASONS,
              verify_maintenance: bool = True, max_minutes: int = 60 * 24 * 7,
              data_dir: Path | None = None, rest: BinanceREST | None = None) -> dict:
    """Догрузить гэпы из реестра. max_minutes — не трогать гэпы длиннее (их разбирать вручную)."""
    data_dir = data_root(data_dir)
    # актуализировать реестр перед догрузкой
    quality.register_gaps(quality.detect_gaps(symbols, data_dir=data_dir))
    todo = _todo(symbols, reasons, verify_maintenance, max_minutes)
    report: dict = {"gaps": len(todo), "bars": 0, "filled": 0, "confirmed_empty": 0, "errors": []}
    if not todo:
        return report
    own = rest is None
    rest = rest or BinanceREST()
    insts = by_symbol()
    empty_ids: list[int] = []
    touched: set[str] = set()
    now_min = now_utc().replace(second=0, microsecond=0)
    try:
        for g in todo:
            inst = insts.get(g["hh_symbol"])
            if inst is None:
                continue
            s, e = g["start_ts"], min(g["end_ts"] + timedelta(minutes=1), now_min)
            if e <= s:
                continue
            try:
                n_mark = rest_klines_range(rest, REST_MARK, inst.binance_um, s, e, data_dir=data_dir)
                n = n_mark
                if g["dataset"] == quality.DATASET:
                    n = rest_klines_range(rest, REST_KLINES, inst.binance_um, s, e, data_dir=data_dir)
            except Exception as ex:  # noqa: BLE001
                report["errors"].append(f"{g['hh_symbol']} {g['dataset']} {s}: {ex}")
                log.error("fill-gaps %s %s %s: %s", g["hh_symbol"], g["dataset"], s, ex)
                continue
            report["bars"] += n
            touched.add(g["hh_symbol"])
            if n == 0:
                empty_ids.append(g["id"])
            log.info("fill-gaps %s %s %s…%s (%d мин, %s): REST баров %d", g["hh_symbol"], g["dataset"], s,
                     g["end_ts"], g["minutes"], g["reason"], n)
    finally:
        if own:
            rest.close()
    for hh in sorted(touched):
        build_symbol(hh, ["bars_1m", "mark_1m", "funding"], data_dir=data_dir)
    # повторный детект: исчезнувшие гэпы → filled
    stats = quality.register_gaps(quality.detect_gaps(symbols, data_dir=data_dir))
    report["filled"] = stats["closed"]
    checked = now_utc().strftime("%Y-%m-%d %H:%M")
    with session_scope() as sess:
        for r in sess.scalars(select(GapRecord).where(GapRecord.id.in_(empty_ids))):
            if r.filled:
                continue
            r.reason = CONFIRMED_REASON
            note = f"REST: биржа не отдаёт бары за период (проверено {checked} UTC)"
            parts = [x for x in (r.detail or "").split("; ") if x and not x.startswith("REST:")]
            r.detail = "; ".join(parts + [note])
            report["confirmed_empty"] += 1
    return report
