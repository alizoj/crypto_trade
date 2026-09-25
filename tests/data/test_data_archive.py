"""Архив data.binance.vision: листинг, строгий шаблон имён, CHECKSUM, raw Parquet, манифест, идемпотентность."""

from __future__ import annotations

import polars as pl
from sqlalchemy import select

from _datahelpers import FakeArchive, klines_csv, kline_lines, make_zip
from quant.data.archive import RemoteFile, plan_files
from quant.data.datasets import DATASETS, raw_path
from quant.db import session_scope
from quant.db.models import ArchiveFile
from quant.timeutil import utc

K = "data/futures/um/monthly/klines/BTCUSDT/1m/"
KD = "data/futures/um/daily/klines/BTCUSDT/1m/"


def _add_month(fake: FakeArchive, month: str, n: int = 30, header: bool = True) -> None:
    y, m = map(int, month.split("-"))
    fake.add(f"{K}BTCUSDT-1m-{month}.zip", make_zip(klines_csv(kline_lines(utc(y, m, 1), n), header=header)))


def test_listing_paginates_and_ignores_foreign_files(tmp_settings, archive_factory):
    fake = FakeArchive(page_size=3)
    for m in ("2024-01", "2024-02", "2024-03"):
        _add_month(fake, m)
    agg = "data/futures/um/monthly/aggTrades/BTCUSDT/"
    fake.add(f"{agg}part-00000-0fae7358-a956-4804-a6d4-1682c07a5127-c000.zip", make_zip("x"))
    fake.add(f"{agg}BTCUSDT-aggTrades-2024-01.zip", make_zip("1,1,1,1,1,1704067200000,true\n"))
    fake.add(f"{agg}BTCUSDT-aggTrades-2024-01-extra.zip", make_zip("x"))
    fake.add(f"{K}BTCUSDT-1m-2024-01.zip.bak", b"x")
    arch = archive_factory(fake)
    kl = arch.list_remote("klines_1m", "BTCUSDT", "monthly")
    assert [r.period for r in kl] == ["2024-01", "2024-02", "2024-03"]     # 6 ключей с CHECKSUM → 2+ страницы
    assert sum("list.test" in u for u in fake.requests) >= 2
    ag = arch.list_remote("aggTrades", "BTCUSDT", "monthly")
    assert [r.name for r in ag] == ["BTCUSDT-aggTrades-2024-01.zip"]
    # marker: листинг с периода
    assert [r.period for r in arch.list_remote("klines_1m", "BTCUSDT", "monthly", start_period="2024-02")] == \
        ["2024-02", "2024-03"]


def test_download_checksum_ok_raw_parquet_and_manifest(tmp_settings, archive_factory):
    fake = FakeArchive()
    _add_month(fake, "2024-01", header=True)
    _add_month(fake, "2024-02", header=False)          # старый формат без заголовка
    arch = archive_factory(fake)
    files = arch.list_remote("klines_1m", "BTCUSDT", "monthly")
    s = arch.sync(files)
    assert (s.downloaded, s.failed) == (2, 0)
    for m in ("2024-01", "2024-02"):
        p = raw_path("klines_1m", "BTCUSDT", m)
        assert p.exists()
        df = pl.read_parquet(p)
        assert df.height == 30 and "open_time" in df.columns
    zips = list((tmp_settings.data_dir / "raw").rglob("*.zip*"))
    assert zips == []                                      # zip удалён после конвертации
    with session_scope() as ss:
        rows = ss.scalars(select(ArchiveFile).order_by(ArchiveFile.period)).all()
        assert [(r.dataset, r.period, r.checksum_ok, r.rows) for r in rows] == \
            [("klines_1m", "2024-01", True, 30), ("klines_1m", "2024-02", True, 30)]
        assert rows[0].sha256 == fake.checksums[f"{K}BTCUSDT-1m-2024-01.zip"]
        assert rows[0].raw_path == "raw/binance_um/klines_1m/BTCUSDT/BTCUSDT-klines_1m-2024-01.parquet"
        assert rows[0].url.endswith("BTCUSDT-1m-2024-01.zip")


def test_rerun_is_idempotent(tmp_settings, archive_factory):
    fake = FakeArchive()
    _add_month(fake, "2024-01")
    arch = archive_factory(fake)
    files = arch.list_remote("klines_1m", "BTCUSDT", "monthly")
    arch.sync(files)
    n = len(fake.downloads())
    s2 = arch.sync(arch.list_remote("klines_1m", "BTCUSDT", "monthly"))
    assert (s2.downloaded, s2.skipped) == (0, 1)
    assert len(fake.downloads()) == n
    # recheck: CHECKSUM совпадает — не перекачивается
    s3 = arch.sync(files, recheck=True)
    assert s3.skipped == 1 and len(fake.downloads()) == n
    # файл на бирже изменился (размер) — перекачивается
    _add_month(fake, "2024-01", n=40)
    s4 = arch.sync(arch.list_remote("klines_1m", "BTCUSDT", "monthly"))
    assert s4.downloaded == 1
    assert pl.read_parquet(raw_path("klines_1m", "BTCUSDT", "2024-01")).height == 40


def test_checksum_mismatch_retries_then_ok(tmp_settings, archive_factory):
    fake = FakeArchive()
    _add_month(fake, "2024-01")
    key = f"{K}BTCUSDT-1m-2024-01.zip"
    fake.corrupt[key] = 1
    arch = archive_factory(fake, retries=3)
    s = arch.sync(arch.list_remote("klines_1m", "BTCUSDT", "monthly"))
    assert (s.downloaded, s.failed) == (1, 0)
    assert len(fake.downloads()) == 2


def test_checksum_mismatch_fails_without_raw(tmp_settings, archive_factory):
    fake = FakeArchive()
    _add_month(fake, "2024-01")
    fake.checksums[f"{K}BTCUSDT-1m-2024-01.zip"] = "0" * 64
    arch = archive_factory(fake, retries=2)
    s = arch.sync(arch.list_remote("klines_1m", "BTCUSDT", "monthly"))
    assert (s.downloaded, s.failed) == (0, 1)
    assert "CHECKSUM" in s.errors[0]
    assert len(fake.downloads()) == 2                      # повтор, потом ошибка
    assert not raw_path("klines_1m", "BTCUSDT", "2024-01").exists()
    with session_scope() as ss:
        row = ss.scalar(select(ArchiveFile))
        assert row is not None and row.checksum_ok is False


def test_keep_zip(tmp_settings, archive_factory):
    fake = FakeArchive()
    _add_month(fake, "2024-01")
    arch = archive_factory(fake, keep_zip=True)
    arch.sync(arch.list_remote("klines_1m", "BTCUSDT", "monthly"))
    assert (raw_path("klines_1m", "BTCUSDT", "2024-01").parent / "BTCUSDT-1m-2024-01.zip").exists()


def _rf(dataset: str, freq: str, period: str) -> RemoteFile:
    return RemoteFile(dataset, "BTCUSDT", freq, period, f"x/{period}.zip", 1)


def test_plan_monthly_for_past_daily_for_current_month():
    spec = DATASETS["klines_1m"]
    monthly = [_rf("klines_1m", "monthly", m) for m in ("2026-07", "2026-08")]
    daily = [_rf("klines_1m", "daily", f"2026-08-{d:02d}") for d in range(1, 32)] + \
            [_rf("klines_1m", "daily", f"2026-09-{d:02d}") for d in range(1, 22)]
    plan = plan_files(spec, monthly, daily, utc(2026, 7, 15), utc(2026, 9, 21))
    periods = [r.period for r in plan]
    assert periods[:2] == ["2026-07", "2026-08"]
    assert periods[2:] == [f"2026-09-{d:02d}" for d in range(1, 21)]      # end исключительно
    # конец внутри месяца: месячный файл не берётся, только дни
    plan2 = plan_files(spec, monthly, daily, utc(2026, 8, 1), utc(2026, 8, 11))
    assert [r.period for r in plan2] == [f"2026-08-{d:02d}" for d in range(1, 11)]
    # только monthly (fundingRate): неполный месяц — месячный файл
    fr = plan_files(DATASETS["fundingRate"], [_rf("fundingRate", "monthly", "2026-08")], [],
                    utc(2026, 8, 1), utc(2026, 8, 20))
    assert [r.period for r in fr] == ["2026-08"]
    # только daily (metrics): с дня start
    mt = plan_files(DATASETS["metrics"], [], [_rf("metrics", "daily", f"2026-08-{d:02d}") for d in range(1, 32)],
                    utc(2026, 8, 9, 13), utc(2026, 8, 12))
    assert [r.period for r in mt] == ["2026-08-09", "2026-08-10", "2026-08-11"]


def test_daily_klines_download(tmp_settings, archive_factory):
    fake = FakeArchive()
    fake.add(f"{KD}BTCUSDT-1m-2026-09-01.zip", make_zip(klines_csv(kline_lines(utc(2026, 9, 1), 5))))
    arch = archive_factory(fake)
    files = arch.plan("klines_1m", "BTCUSDT", utc(2026, 9, 1), utc(2026, 9, 2))
    assert [f.period for f in files] == ["2026-09-01"]
    assert arch.sync(files).downloaded == 1
    assert raw_path("klines_1m", "BTCUSDT", "2026-09-01").exists()


def test_big_csv_streaming_path_cleans_extract(tmp_settings, archive_factory, monkeypatch):
    import quant.data.archive as archive_mod

    monkeypatch.setattr(archive_mod, "BIG_CSV_BYTES", 10)                 # любой CSV — «большой»
    fake = FakeArchive()
    agg = "data/futures/um/monthly/aggTrades/BTCUSDT/"
    rows = "".join(f"{i},100.{i},0.5,{i},{i},{1704067200000 + i},{'true' if i % 2 else 'false'}\n" for i in range(50))
    fake.add(f"{agg}BTCUSDT-aggTrades-2024-01.zip", make_zip(rows))
    arch = archive_factory(fake)
    assert arch.sync(arch.list_remote("aggTrades", "BTCUSDT", "monthly")).downloaded == 1
    p = raw_path("aggTrades", "BTCUSDT", "2024-01")
    df = pl.read_parquet(p)
    assert df.height == 50 and df["is_buyer_maker"].sum() == 25
    assert [x.name for x in p.parent.iterdir()] == [p.name]              # ни .extract, ни .tmp, ни zip


def test_plan_skips_listing_for_months_known_from_manifest(tmp_settings, archive_factory):
    fake = FakeArchive()
    md = "data/futures/um/daily/metrics/BTCUSDT/"
    hdr = ("create_time,symbol,sum_open_interest,sum_open_interest_value,count_toptrader_long_short_ratio,"
           "sum_toptrader_long_short_ratio,count_long_short_ratio,sum_taker_long_short_vol_ratio\n")
    for d in (1, 2, 3):
        fake.add(f"{md}BTCUSDT-metrics-2024-01-0{d}.zip",
                 make_zip(hdr + f"2024-01-0{d} 00:05:00,BTCUSDT,1,2,1,1,1,1\n"))
    arch = archive_factory(fake)
    files = arch.plan("metrics", "BTCUSDT", utc(2024, 1, 1), utc(2024, 1, 4))
    assert [f.period for f in files] == ["2024-01-01", "2024-01-02", "2024-01-03"]
    assert arch.sync(files).downloaded == 3
    n_list = sum("list.test" in u for u in fake.requests)
    again = arch.plan("metrics", "BTCUSDT", utc(2024, 1, 1), utc(2024, 1, 4))
    assert [f.period for f in again] == ["2024-01-01", "2024-01-02", "2024-01-03"]
    assert sum("list.test" in u for u in fake.requests) == n_list          # прошлый месяц не листается
    assert arch.sync(again).skipped == 3
