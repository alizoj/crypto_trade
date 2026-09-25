"""Архив data.binance.vision → raw Parquet + манифест (3.2.4).

- Листинг S3 (`?prefix=…`, пагинация marker/IsTruncated, ≤1000 ключей на страницу).
- Только файлы строго по шаблону `<SYMBOL>-<dataset>-<period>.zip`; посторонние
  (например `aggTrades/BTCUSDT/part-00000-….zip`) игнорируются.
- Каждый zip сверяется с `.CHECKSUM` (SHA-256); при несовпадении — повторная загрузка, затем ошибка.
- zip → raw Parquet «как пришло» (quant.data.parse), zip удаляется (keep_zip — оставить).
- Манифест — таблица ArchiveFile (url, sha256, checksum_ok, размер zip, rows, raw_path относительно data_dir).
- Повторный запуск идемпотентен: файл пропускается, если в манифесте checksum_ok, raw на месте и размер
  zip в листинге не изменился (recheck=True — дополнительно заново сверить .CHECKSUM).
"""

from __future__ import annotations

import gc
import hashlib
import logging
import os
import shutil
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import polars as pl
from sqlalchemy import select

from quant.data import parse
from quant.data.datasets import DATASETS, SOURCE_BINANCE_UM, DatasetSpec, data_root, raw_dir, raw_path
from quant.db import session_scope
from quant.db.models import ArchiveFile, utcnow
from quant.timeutil import month_range

log = logging.getLogger(__name__)

S3_LIST_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
DOWNLOAD_URL = "https://data.binance.vision"
BIG_CSV_BYTES = 256 * 1024 * 1024     # крупнее — распаковка на диск и потоковая конвертация


class ArchiveError(RuntimeError):
    pass


class NotFound(ArchiveError):
    pass


@dataclass(frozen=True)
class RemoteFile:
    dataset: str
    source_symbol: str
    freq: str            # monthly | daily
    period: str          # YYYY-MM | YYYY-MM-DD
    key: str
    size: int

    @property
    def name(self) -> str:
        return self.key.rsplit("/", 1)[-1]

    def bounds(self) -> tuple[datetime, datetime]:
        if self.freq == "monthly":
            y, m = map(int, self.period.split("-"))
            s = datetime(y, m, 1, tzinfo=timezone.utc)
            return s, datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=timezone.utc)
        s = datetime.strptime(self.period, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return s, s + timedelta(days=1)


@dataclass
class FileResult:
    rf: RemoteFile
    status: str                  # downloaded | skipped | failed
    sha256: str = ""
    checksum_ok: bool = False
    rows: int = 0
    raw_rel: str = ""
    error: str = ""


@dataclass
class SyncSummary:
    total: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    bytes: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"total": self.total, "downloaded": self.downloaded, "skipped": self.skipped,
                "failed": self.failed, "mb": round(self.bytes / 1e6, 1), "errors": self.errors[:20]}


# ---------------------------------------------------------------- план файлов

def plan_files(spec: DatasetSpec, monthly: list[RemoteFile], daily: list[RemoteFile],
               start: datetime, end: datetime) -> list[RemoteFile]:
    """Monthly — для месяцев, целиком закрытых внутри диапазона; daily — для остальных дней [start, end).

    Датасеты только с monthly (fundingRate) берут месячный файл и для неполного месяца.
    """
    if end <= start:
        return []
    mmap = {rf.period: rf for rf in monthly}
    dmap: dict[str, list[RemoteFile]] = {}
    for rf in daily:
        dmap.setdefault(rf.period[:7], []).append(rf)
    day0 = start.replace(hour=0, minute=0, second=0, microsecond=0)
    out: list[RemoteFile] = []
    for m in month_range(start, end - timedelta(microseconds=1)):
        mf = mmap.get(m)
        full = mf is not None and mf.bounds()[1] <= end
        if spec.monthly and mf is not None and (full or not spec.daily):
            out.append(mf)
            continue
        if spec.daily:
            for rf in sorted(dmap.get(m, []), key=lambda r: r.period):
                s, _ = rf.bounds()
                if day0 <= s < end:
                    out.append(rf)
    return out


# ---------------------------------------------------------------- клиент архива

class BinanceArchive:
    def __init__(self, *, data_dir: Path | None = None, workers: int = 6, keep_zip: bool = False,
                 retries: int = 3, transport: httpx.BaseTransport | None = None,
                 list_url: str = S3_LIST_URL, download_url: str = DOWNLOAD_URL, timeout: float = 60.0,
                 backoff: float = 1.0):
        self.data_dir = data_root(data_dir)
        self.workers = max(1, workers)
        self.keep_zip = keep_zip
        self.retries = max(1, retries)
        self.transport = transport
        self.list_url = list_url.rstrip("/")
        self.download_url = download_url.rstrip("/")
        self.timeout = timeout
        self.backoff = backoff
        self._local = threading.local()

    # --- http
    def _client(self) -> httpx.Client:
        c = getattr(self._local, "client", None)
        if c is None:
            c = httpx.Client(timeout=httpx.Timeout(self.timeout, connect=20.0), transport=self.transport,
                             follow_redirects=True, headers={"User-Agent": "hashhedge-quant/0.1"})
            self._local.client = c
        return c

    def _get(self, url: str, params: dict | None = None) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(self.retries + 2):
            try:
                r = self._client().get(url, params=params)
            except httpx.TransportError as e:
                last = e
            else:
                if r.status_code == 404:
                    raise NotFound(url)
                if r.status_code < 400:
                    return r
                last = ArchiveError(f"HTTP {r.status_code} {url}")
                if r.status_code < 500 and r.status_code != 429:
                    raise last
            time.sleep(min(30.0, self.backoff * 2 ** attempt))
        raise ArchiveError(f"GET {url}: ретраи исчерпаны: {last}")

    def url(self, rf: RemoteFile) -> str:
        return f"{self.download_url}/{rf.key}"

    # --- листинг
    def list_keys(self, prefix: str, *, marker: str = "") -> list[tuple[str, int]]:
        out: list[tuple[str, int]] = []
        while True:
            params = {"prefix": prefix}
            if marker:
                params["marker"] = marker
            root = ET.fromstring(self._get(self.list_url, params).content)
            ns = root.tag[: root.tag.index("}") + 1] if root.tag.startswith("{") else ""
            contents = root.findall(f"{ns}Contents")
            for c in contents:
                out.append((c.findtext(f"{ns}Key") or "", int(c.findtext(f"{ns}Size") or 0)))
            truncated = (root.findtext(f"{ns}IsTruncated") or "false").lower() == "true"
            if not truncated or not contents:
                break
            marker = root.findtext(f"{ns}NextMarker") or out[-1][0]
        return out

    def list_remote(self, dataset: str, source_symbol: str, freq: str, *,
                    start_period: str | None = None, only_period: str | None = None) -> list[RemoteFile]:
        """Файлы датасета. start_period — листинг с периода (marker); only_period — только файлы,
        чьё имя начинается с периода (например месяц 'YYYY-MM' для daily — узкий prefix)."""
        spec = DATASETS[dataset]
        prefix = spec.prefix(freq, source_symbol)
        stem = f"{prefix}{source_symbol}-{spec.file_tag}-"
        marker = f"{stem}{start_period}" if start_period else ""
        if only_period:
            prefix, marker = f"{stem}{only_period}", ""
        pat = spec.file_re(source_symbol, freq)
        out = []
        for key, size in self.list_keys(prefix, marker=marker):
            name = key.rsplit("/", 1)[-1]
            m = pat.match(name)
            if m:
                out.append(RemoteFile(dataset, source_symbol, freq, m.group(1), key, size))
        return out

    def _known_daily(self, dataset: str, source_symbol: str) -> dict[str, RemoteFile]:
        """Дневные файлы, уже загруженные и сверенные (из манифеста), — чтобы не листать их месяцы заново."""
        out: dict[str, RemoteFile] = {}
        with session_scope() as s:
            q = select(ArchiveFile).where(ArchiveFile.source == SOURCE_BINANCE_UM, ArchiveFile.dataset == dataset,
                                          ArchiveFile.source_symbol == source_symbol,
                                          ArchiveFile.checksum_ok.is_(True))
            for row in s.scalars(q):
                if len(row.period) != 10 or not row.url.startswith(self.download_url + "/"):
                    continue
                key = row.url[len(self.download_url) + 1:]
                out[row.period] = RemoteFile(dataset, source_symbol, "daily", row.period, key, row.size_bytes)
        return out

    def plan(self, dataset: str, source_symbol: str, start: datetime, end: datetime) -> list[RemoteFile]:
        """Список файлов к загрузке за [start, end).

        Листинг S3 медленный и объёмный (XML на каждый ключ), поэтому: monthly — один запрос с marker;
        daily — узкий prefix по каждому нужному месяцу (параллельно); прошедшие месяцы, все дни которых
        уже есть в манифесте, не листаются вовсе.
        """
        spec = DATASETS[dataset]
        sp_m = f"{start.year:04d}-{start.month:02d}"
        monthly = self.list_remote(dataset, source_symbol, "monthly", start_period=sp_m) if spec.monthly else []
        if not spec.daily:
            return plan_files(spec, monthly, [], start, end)
        mmap = {rf.period: rf for rf in monthly}
        known = self._known_daily(dataset, source_symbol)
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        day0 = start.replace(hour=0, minute=0, second=0, microsecond=0)
        daily: list[RemoteFile] = []
        to_list: list[str] = []
        for m in month_range(start, end - timedelta(microseconds=1)):
            mf = mmap.get(m)
            if mf is not None and mf.bounds()[1] <= end:
                continue                                   # месяц закрыт месячным файлом
            y, mo = map(int, m.split("-"))
            m_start = datetime(y, mo, 1, tzinfo=timezone.utc)
            m_end = datetime(y + (mo == 12), mo % 12 + 1, 1, tzinfo=timezone.utc)
            days, d = [], max(m_start, day0)
            while d < min(m_end, end):
                days.append(d.date().isoformat())
                d += timedelta(days=1)
            if days and min(m_end, end) <= today and all(x in known for x in days):
                daily += [known[x] for x in days]
            elif days:
                to_list.append(m)
        if to_list:
            with ThreadPoolExecutor(max_workers=min(4, len(to_list))) as ex:
                for res in ex.map(lambda mm: self.list_remote(dataset, source_symbol, "daily", only_period=mm),
                                  to_list):
                    daily += res
        return plan_files(spec, monthly, daily, start, end)

    # --- загрузка одного файла
    def fetch_checksum(self, rf: RemoteFile) -> str:
        text = self._get(self.url(rf) + ".CHECKSUM").text.strip()
        sha = text.split()[0].lower() if text else ""
        if len(sha) != 64:
            raise ArchiveError(f"{rf.name}: некорректный CHECKSUM {text[:80]!r}")
        return sha

    def _download_zip(self, rf: RemoteFile, dest: Path) -> str:
        h = hashlib.sha256()
        with self._client().stream("GET", self.url(rf)) as r:
            if r.status_code == 404:
                raise NotFound(self.url(rf))
            if r.status_code >= 400:
                raise ArchiveError(f"HTTP {r.status_code} {self.url(rf)}")
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    h.update(chunk)
                    f.write(chunk)
        return h.hexdigest()

    def download_file(self, rf: RemoteFile) -> FileResult:
        spec = DATASETS[rf.dataset]
        d = raw_dir(rf.dataset, rf.source_symbol, data_dir=self.data_dir)
        d.mkdir(parents=True, exist_ok=True)
        try:
            expected = self.fetch_checksum(rf)
        except Exception as e:  # noqa: BLE001
            return FileResult(rf, "failed", error=f"CHECKSUM: {e}")
        part = d / (rf.name + ".part")
        sha = ""
        for attempt in range(self.retries):
            try:
                sha = self._download_zip(rf, part)
            except (httpx.HTTPError, ArchiveError) as e:
                log.warning("%s: загрузка не удалась (%s), попытка %d", rf.name, e, attempt + 1)
                sha = ""
            if sha == expected:
                break
            if sha:
                log.warning("%s: sha256 %s != CHECKSUM %s, попытка %d", rf.name, sha[:12], expected[:12], attempt + 1)
            time.sleep(min(30.0, self.backoff * 2 ** attempt))
        else:
            part.unlink(missing_ok=True)
            return FileResult(rf, "failed", sha256=sha, checksum_ok=False,
                              error=f"{rf.name}: sha256 не совпал с CHECKSUM после {self.retries} попыток")
        out = raw_path(rf.dataset, rf.source_symbol, rf.period, data_dir=self.data_dir)
        try:
            rows = convert_zip(part, spec.kind, out)
        except Exception as e:  # noqa: BLE001
            part.unlink(missing_ok=True)
            return FileResult(rf, "failed", sha256=sha, checksum_ok=True, error=f"{rf.name}: разбор: {e}")
        if self.keep_zip:
            os.replace(part, d / rf.name)
        else:
            part.unlink(missing_ok=True)
        return FileResult(rf, "downloaded", sha256=sha, checksum_ok=True, rows=rows,
                          raw_rel=out.relative_to(self.data_dir).as_posix())

    def _cleanup(self, pairs: set[tuple[str, str]]) -> None:
        """Хвосты прерванных запусков: *.zip.part, *.parquet.tmp, *.extract."""
        for dataset, sym in pairs:
            d = raw_dir(dataset, sym, data_dir=self.data_dir)
            if not d.exists():
                continue
            for p in list(d.glob("*.part")) + list(d.glob("*.tmp")):
                p.unlink(missing_ok=True)
            for p in d.glob("*.extract"):
                _rmtree(p)

    def _recheck(self, rf: RemoteFile, known_sha: str) -> bool:
        try:
            return self.fetch_checksum(rf) == known_sha
        except Exception:  # noqa: BLE001
            return False

    # --- пакетная синхронизация
    def sync(self, files: list[RemoteFile], *, recheck: bool = False, label: str = "") -> SyncSummary:
        summary = SyncSummary(total=len(files))
        if not files:
            return summary
        known = load_manifest(files)
        todo: list[RemoteFile] = []
        to_recheck: list[tuple[RemoteFile, str]] = []
        for rf in files:
            row = known.get((rf.dataset, rf.source_symbol, rf.period))
            ok = (row is not None and row["checksum_ok"] and row["size_bytes"] == rf.size
                  and (self.data_dir / row["raw_path"]).exists())
            if ok and recheck:
                to_recheck.append((rf, row["sha256"]))
            elif ok:
                summary.skipped += 1
            else:
                todo.append(rf)
        self._cleanup({(rf.dataset, rf.source_symbol) for rf in todo})
        pending: list[FileResult] = []
        t_log = time.monotonic()
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            if to_recheck:
                futs = {ex.submit(self._recheck, rf, sha): rf for rf, sha in to_recheck}
                for fut in as_completed(futs):
                    if fut.result():
                        summary.skipped += 1
                    else:
                        todo.append(futs[fut])
            log.info("архив%s: файлов %d, к загрузке %d, пропущено %d", f" {label}" if label else "",
                     len(files), len(todo), summary.skipped)
            futs2 = [ex.submit(self.download_file, rf) for rf in todo]
            for fut in as_completed(futs2):
                res = fut.result()
                if res.status == "downloaded":
                    summary.downloaded += 1
                    summary.bytes += res.rf.size
                else:
                    summary.failed += 1
                    summary.errors.append(res.error)
                    log.error("архив: %s", res.error)
                pending.append(res)
                if len(pending) >= 200:
                    record_manifest(pending, self)
                    pending = []
                if time.monotonic() - t_log > 10:
                    t_log = time.monotonic()
                    log.info("архив%s: %d/%d загружено, %.1f МБ, ошибок %d", f" {label}" if label else "",
                             summary.downloaded, len(todo), summary.bytes / 1e6, summary.failed)
        record_manifest(pending, self)
        return summary


# ---------------------------------------------------------------- конвертация

def convert_zip(zip_path: Path, kind: str, out: Path) -> int:
    """zip (один CSV) → raw Parquet (атомарно). Возвращает число строк."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".csv")] or z.namelist()
        if len(names) != 1:
            raise ArchiveError(f"{zip_path.name}: ожидался один CSV, есть {names}")
        info = z.getinfo(names[0])
        if info.file_size <= BIG_CSV_BYTES:
            df = parse.parse_bytes(z.read(names[0]), kind)
            df.write_parquet(tmp, compression="zstd", statistics=True)
            rows = df.height
        else:
            xdir = out.parent / (out.stem + ".extract")
            xdir.mkdir(exist_ok=True)
            try:
                csv_path = Path(z.extract(names[0], xdir))
                parse.scan_file(csv_path, kind).sink_parquet(tmp, compression="zstd")
            finally:
                _rmtree(xdir)
            rows = int(pl.scan_parquet(tmp).select(pl.len()).collect().item())
    os.replace(tmp, out)
    return rows


def _rmtree(d: Path) -> None:
    """Удалить распакованный CSV. На Windows файл может быть ещё занят (mmap/антивирус) — ретраи."""
    for attempt in range(10):
        gc.collect()
        shutil.rmtree(d, ignore_errors=True)
        if not d.exists():
            return
        time.sleep(0.5 * (attempt + 1))
    log.warning("не удалось удалить %s — будет удалён при следующем sync", d)


# ---------------------------------------------------------------- манифест

def load_manifest(files: list[RemoteFile], source: str = SOURCE_BINANCE_UM) -> dict[tuple[str, str, str], dict]:
    datasets = sorted({f.dataset for f in files})
    symbols = sorted({f.source_symbol for f in files})
    out: dict[tuple[str, str, str], dict] = {}
    with session_scope() as s:
        q = select(ArchiveFile).where(ArchiveFile.source == source, ArchiveFile.dataset.in_(datasets),
                                      ArchiveFile.source_symbol.in_(symbols))
        for row in s.scalars(q):
            out[(row.dataset, row.source_symbol, row.period)] = {
                "sha256": row.sha256, "checksum_ok": row.checksum_ok, "size_bytes": row.size_bytes,
                "raw_path": row.raw_path, "rows": row.rows}
    return out


def record_manifest(results: list[FileResult], archive: BinanceArchive, source: str = SOURCE_BINANCE_UM) -> None:
    results = [r for r in results if r.status != "skipped"]
    if not results:
        return
    with session_scope() as s:
        for r in results:
            rf = r.rf
            row = s.scalar(select(ArchiveFile).where(
                ArchiveFile.source == source, ArchiveFile.dataset == rf.dataset,
                ArchiveFile.source_symbol == rf.source_symbol, ArchiveFile.period == rf.period))
            if row is None:
                row = ArchiveFile(source=source, dataset=rf.dataset, source_symbol=rf.source_symbol,
                                  period=rf.period)
                s.add(row)
            elif r.status == "failed" and row.checksum_ok and not r.checksum_ok and \
                    (archive.data_dir / row.raw_path).exists():
                # старая корректная копия остаётся источником истины — не затираем её отметку
                continue
            row.url = archive.url(rf)
            row.sha256 = r.sha256
            row.checksum_ok = r.checksum_ok and r.status == "downloaded"
            row.size_bytes = rf.size
            row.rows = r.rows
            row.raw_path = r.raw_rel
            row.downloaded_at = utcnow()
