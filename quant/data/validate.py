"""Валидация мастер-листа инструментов по биржам (правило 3.1.5).

Проверки по каждому инструменту:
- Binance USDⓈ-M: тикер существует и TRADING; onboardDate == binance_onboard; множитель (1000-контракты,
  по префиксу baseAsset) == binance_um_mult; underlyingType == underlying_type (предупреждение);
- Bybit linear: тикер существует и Trading (закрытые запрашиваются по symbol=...); launchTime == bybit_launch;
  множитель по имени символа (1000PEPEUSDT, SHIB1000USDT) == bybit_linear_mult;
- пара Binance↔Bybit: медиана |p_b/mult_b − p_y/mult_y| / (p_b/mult_b) по 1m close за последние 24 ч
  меньше порога группы (crypto_major и tradfi 0.3%, crypto_alt 0.6%), иначе mapping_suspect;
- Binance spot (api/v3/exchangeInfo): тикер существует и TRADING (только предупреждение — спот лишь для прогрева).
Смена onboardDate/launchTime (переименование, релистинг, переиспользование тикера) → issue onboard_changed и
signals_blocked до ручного обновления history_start. Результат — InstrumentStatus + отчёт в консоль.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

import polars as pl

from quant.data.rest import BinanceREST, BybitREST
from quant.db import session_scope
from quant.db.models import InstrumentStatus, utcnow
from quant.instruments import Instrument, instruments
from quant.timeutil import now_utc, to_ms

log = logging.getLogger(__name__)

DIVERGENCE_THRESHOLDS_PCT = {"crypto_major": 0.3, "tradfi": 0.3, "crypto_alt": 0.6}
DIVERGENCE_WINDOW = timedelta(hours=24)

_PREFIX = re.compile(r"^(1000000|100000|10000|1000|1M)(?=[A-Z])")
_SUFFIX = re.compile(r"(?<=[A-Z])(1000000|100000|10000|1000)(?=(USDT|USDC|PERP)?$)")


def symbol_multiplier(name: str) -> int:
    """Множитель контракта по имени: 1000PEPE(USDT) → 1000, 1MBABYDOGE → 1e6, SHIB1000USDT → 1000, иначе 1."""
    m = _PREFIX.match(name)
    if m:
        return 1_000_000 if m.group(1) == "1M" else int(m.group(1))
    m = _SUFFIX.search(name)
    if m:
        return int(m.group(1))
    return 1


@dataclass
class Issue:
    code: str
    severity: str            # error | warn
    detail: str


@dataclass
class ValidationResult:
    hh_symbol: str
    group: str
    issues: list[Issue] = field(default_factory=list)
    mapping_suspect: bool = False
    signals_blocked: bool = False
    divergence_median_pct: float | None = None

    @property
    def ok(self) -> bool:
        return not self.signals_blocked and not self.mapping_suspect and \
            not any(i.severity == "error" for i in self.issues)

    def add(self, code: str, severity: str, detail: str, *, block: bool = False, suspect: bool = False) -> None:
        self.issues.append(Issue(code, severity, detail))
        self.signals_blocked |= block
        self.mapping_suspect |= suspect


def _same_second(ms: int | str | None, dt: datetime | None) -> bool:
    if ms in (None, "", 0, "0") or dt is None:
        return ms in (None, "", 0, "0") and dt is None
    return int(ms) // 1000 == int(dt.timestamp())


def _fmt_ms(ms: int | str | None) -> str:
    if ms in (None, "", 0, "0"):
        return "-"
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def median_divergence_pct(binance_rows: list[list], bybit_rows: list[list], mult_b: float, mult_y: float) -> float | None:
    """Медиана относительного расхождения нормализованных 1m close (в %). None — нет общих минут."""
    if not binance_rows or not bybit_rows:
        return None
    b = pl.DataFrame({"ts": [int(r[0]) for r in binance_rows], "pb": [float(r[4]) / mult_b for r in binance_rows]})
    y = pl.DataFrame({"ts": [int(r[0]) for r in bybit_rows], "py": [float(r[4]) / mult_y for r in bybit_rows]})
    j = b.join(y, on="ts", how="inner")
    if j.is_empty():
        return None
    return float(j.select(((pl.col("pb") - pl.col("py")).abs() / pl.col("pb") * 100).median()).item())


def validate(items: list[Instrument] | None = None, *, check_divergence: bool = True,
             thresholds: dict[str, float] | None = None, workers: int = 8,
             binance: BinanceREST | None = None, bybit: BybitREST | None = None,
             persist: bool = True) -> list[ValidationResult]:
    items = list(items) if items is not None else list(instruments())
    thr = {**DIVERGENCE_THRESHOLDS_PCT, **(thresholds or {})}
    own_b, own_y = binance is None, bybit is None
    binance = binance or BinanceREST()
    bybit = bybit or BybitREST()
    try:
        fut_info = {s["symbol"]: s for s in binance.exchange_info()["symbols"]}
        try:
            spot_info = {s["symbol"]: s for s in binance.spot_exchange_info()["symbols"]}
        except Exception as e:  # noqa: BLE001
            log.warning("spot exchangeInfo недоступен: %s", e)
            spot_info = None
        by_info = {s["symbol"]: s for s in bybit.instruments(category="linear")}
        for inst in items:   # закрытые/PreLaunch не всегда в общем списке — спрашиваем точечно
            if inst.bybit_linear and inst.bybit_linear not in by_info:
                for s in bybit.instruments(category="linear", symbol=inst.bybit_linear):
                    by_info[s["symbol"]] = s

        results = [_check_meta(inst, fut_info, by_info, spot_info) for inst in items]
        if check_divergence:
            end = now_utc().replace(second=0, microsecond=0) - timedelta(minutes=1)
            start = end - DIVERGENCE_WINDOW
            pairs = [(inst, res) for inst, res in zip(items, results)
                     if inst.bybit_linear and fut_info.get(inst.binance_um, {}).get("status") == "TRADING"
                     and by_info.get(inst.bybit_linear, {}).get("status") == "Trading"]

            def job(pair):
                inst, _ = pair
                b = binance.klines(inst.binance_um, to_ms(start), to_ms(end))
                y = bybit.kline(inst.bybit_linear, to_ms(start), to_ms(end))
                return median_divergence_pct(b, y, inst.binance_um_mult, inst.bybit_linear_mult or 1.0)

            with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
                futs = [(pair, ex.submit(job, pair)) for pair in pairs]
                for (inst, res), fut in futs:
                    try:
                        med = fut.result()
                    except Exception as e:  # noqa: BLE001
                        res.add("divergence_unavailable", "warn", f"нет данных для сверки цены: {e}")
                        continue
                    res.divergence_median_pct = med
                    limit = thr[inst.group]
                    if med is None:
                        res.add("divergence_unavailable", "warn", "нет общих минут Binance/Bybit за 24 ч")
                    elif med > limit:
                        res.add("mapping_suspect", "error",
                                f"медиана расхождения {med:.3f}% > {limit}% ({inst.group})", suspect=True)
    finally:
        if own_b:
            binance.close()
        if own_y:
            bybit.close()
    if persist:
        save_status(results)
    return results


def _check_meta(inst: Instrument, fut_info: dict, by_info: dict, spot_info: dict | None) -> ValidationResult:
    res = ValidationResult(inst.hh_symbol, inst.group)
    # Binance USDⓈ-M (основной источник)
    bi = fut_info.get(inst.binance_um)
    if bi is None:
        res.add("binance_missing", "error", f"{inst.binance_um} нет в exchangeInfo", block=True)
    else:
        if bi.get("status") != "TRADING":
            res.add("binance_not_trading", "error", f"{inst.binance_um}: status={bi.get('status')}", block=True)
        if not _same_second(bi.get("onboardDate"), inst.binance_onboard):
            res.add("onboard_changed", "error",
                    f"Binance onboardDate {_fmt_ms(bi.get('onboardDate'))} != CSV "
                    f"{inst.binance_onboard.strftime('%Y-%m-%dT%H:%M:%SZ') if inst.binance_onboard else '-'}",
                    block=True)
        mult = symbol_multiplier(bi.get("baseAsset") or inst.binance_um)
        if mult != inst.binance_um_mult:
            res.add("multiplier_mismatch", "error",
                    f"Binance {inst.binance_um}: множитель {mult} != CSV {inst.binance_um_mult:g}", block=True)
        ut = bi.get("underlyingType")
        if ut and inst.underlying_type and ut != inst.underlying_type:
            res.add("underlying_changed", "warn", f"underlyingType {ut} != CSV {inst.underlying_type}")
    # Bybit linear (вторичный источник)
    if inst.bybit_linear:
        yi = by_info.get(inst.bybit_linear)
        if yi is None:
            res.add("bybit_missing", "warn", f"{inst.bybit_linear} нет на Bybit", suspect=True)
        else:
            if yi.get("status") != "Trading":
                res.add("bybit_not_trading", "warn", f"{inst.bybit_linear}: status={yi.get('status')}", suspect=True)
            if not _same_second(yi.get("launchTime"), inst.bybit_launch):
                res.add("onboard_changed", "error",
                        f"Bybit launchTime {_fmt_ms(yi.get('launchTime'))} != CSV "
                        f"{inst.bybit_launch.strftime('%Y-%m-%dT%H:%M:%SZ') if inst.bybit_launch else '-'}",
                        block=True)
            ym = symbol_multiplier(inst.bybit_linear)
            if inst.bybit_linear_mult is not None and ym != inst.bybit_linear_mult:
                res.add("multiplier_mismatch", "error",
                        f"Bybit {inst.bybit_linear}: множитель {ym} != CSV {inst.bybit_linear_mult:g}", suspect=True)
    # Binance spot (только прогрев индикаторов)
    if inst.binance_spot and spot_info is not None:
        si = spot_info.get(inst.binance_spot)
        if si is None:
            res.add("spot_missing", "warn", f"spot {inst.binance_spot} нет в api/v3/exchangeInfo")
        elif si.get("status") != "TRADING":
            res.add("spot_not_trading", "warn", f"spot {inst.binance_spot}: status={si.get('status')}")
    return res


def save_status(results: list[ValidationResult]) -> None:
    with session_scope() as s:
        for r in results:
            s.merge(InstrumentStatus(
                hh_symbol=r.hh_symbol, checked_at=utcnow(), ok=r.ok, mapping_suspect=r.mapping_suspect,
                signals_blocked=r.signals_blocked, issues=[asdict(i) for i in r.issues],
                divergence_median_pct=r.divergence_median_pct))


def format_report(results: list[ValidationResult]) -> str:
    ok = sum(r.ok for r in results)
    lines = [f"Валидация инструментов: {len(results)} всего, ok {ok}, с замечаниями {len(results) - ok}"]
    blocked = [r.hh_symbol for r in results if r.signals_blocked]
    suspect = [r.hh_symbol for r in results if r.mapping_suspect]
    if blocked:
        lines.append(f"  signals_blocked ({len(blocked)}): {', '.join(blocked)}")
    if suspect:
        lines.append(f"  mapping_suspect ({len(suspect)}): {', '.join(suspect)}")
    codes: dict[str, int] = {}
    for r in results:
        for i in r.issues:
            codes[i.code] = codes.get(i.code, 0) + 1
    if codes:
        lines.append("  issues: " + ", ".join(f"{k}={v}" for k, v in sorted(codes.items())))
    for r in results:
        for i in r.issues:
            lines.append(f"  [{i.severity}] {r.hh_symbol}: {i.code} — {i.detail}")
    divs = sorted((r.divergence_median_pct, r.hh_symbol, r.group) for r in results
                  if r.divergence_median_pct is not None)
    if divs:
        lines.append(f"  расхождение Binance↔Bybit (медиана 1m за 24 ч): пар {len(divs)}, "
                     f"макс {divs[-1][0]:.3f}% ({divs[-1][1]}, {divs[-1][2]})")
        for grp in ("crypto_major", "crypto_alt", "tradfi"):
            g = [d for d in divs if d[2] == grp]
            if g:
                lines.append(f"    {grp}: пар {len(g)}, макс {g[-1][0]:.3f}% ({g[-1][1]}), "
                             f"медиана {g[len(g) // 2][0]:.3f}%")
    return "\n".join(lines)
