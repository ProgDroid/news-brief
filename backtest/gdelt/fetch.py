# backtest/gdelt/fetch.py
"""Download + unzip GDELT 1.0 daily Event files, with an on-disk cache. The
network fetch is operator-run (not exercised in CI); unzip_to_rows and the
cache-hit path are pure/deterministic and tested. 404 / any network error
-> [] (a missing day skips, never raises)."""

import csv
import io
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

_BASE = "http://data.gdeltproject.org/events"


def events_url(date_yyyymmdd: str) -> str:
    return f"{_BASE}/{date_yyyymmdd}.export.CSV.zip"


def unzip_to_rows(zip_bytes: bytes) -> list[list[str]]:
    """First member of the zip parsed as tab-delimited rows (blank rows dropped)."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        text = zf.read(zf.namelist()[0]).decode("utf-8", errors="replace")
    return [r for r in csv.reader(io.StringIO(text), delimiter="\t") if r]


def fetch_day_rows(
    date_yyyymmdd: str, *, cache_dir: str | None = None, timeout: float = 60.0
) -> list[list[str]]:
    """Rows for one GDELT day. Caches the raw zip under cache_dir so re-runs are
    free. Returns [] on 404 (day not published) or any network/zip error."""
    cache = Path(cache_dir) / f"{date_yyyymmdd}.export.CSV.zip" if cache_dir else None
    if cache and cache.exists():
        data = cache.read_bytes()
    else:
        try:
            with urlopen(events_url(date_yyyymmdd), timeout=timeout) as r:  # noqa: S310
                data = r.read()
        except (HTTPError, URLError, TimeoutError):
            return []
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(data)
    try:
        return unzip_to_rows(data)
    except (zipfile.BadZipFile, OSError):
        return []
