from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def clean_value(value: Any) -> Any:
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return value


def row_payload(row: pd.Series) -> str:
    return json.dumps({k: clean_value(v) for k, v in row.to_dict().items()}, ensure_ascii=False)


def read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False).replace({"": None})


def safe_float(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "" or str(value).strip() in {"-", "–", "nan", "None"}:
        return default
    try:
        return float(str(value).replace("£", "").replace(",", "").strip())
    except Exception:
        return default


def safe_int(value: Any, default: int | None = None) -> int | None:
    f = safe_float(value, None)
    if f is None:
        return default
    return int(f)


def parse_date_from_df(df: pd.DataFrame, fallback: str = "unknown") -> str:
    for col in ["race_date", "date"]:
        if col in df.columns and df[col].notna().any():
            return str(df[col].dropna().iloc[0])[:10]
    return fallback
