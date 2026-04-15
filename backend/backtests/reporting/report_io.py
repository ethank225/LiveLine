"""Shared helpers for writing timestamped analysis reports.

`results/` holds raw per-game sync CSVs produced during data collection.
`reports/` holds derived analysis outputs (terminal transcripts + any
trade-level CSVs from analysis passes). Every report filename is suffixed
with a UTC timestamp so repeated runs never overwrite each other.
"""

import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# `reports/` lives at the backtests package root (backtests/reports/),
# one level above this reporting/ subpackage.
REPORTS_DIR = Path(__file__).resolve().parents[1] / "reports"


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")


def timestamped_path(name: str, ext: str = "csv") -> Path:
    """Build a timestamped path inside REPORTS_DIR. Does not create the file."""
    REPORTS_DIR.mkdir(exist_ok=True)
    return REPORTS_DIR / f"{name}_{_ts()}.{ext.lstrip('.')}"


class _Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            st.write(s)

    def flush(self):
        for st in self.streams:
            try:
                st.flush()
            except Exception:
                pass


@contextmanager
def capture_report(name: str):
    """Tee stdout to a timestamped .txt in REPORTS_DIR for the block's duration.

    Yields the report path so callers can print or return it.
    """
    path = timestamped_path(name, "txt")
    orig = sys.stdout
    with open(path, "w") as f:
        sys.stdout = _Tee(orig, f)
        try:
            yield path
        finally:
            sys.stdout = orig
