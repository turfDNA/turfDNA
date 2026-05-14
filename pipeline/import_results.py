from __future__ import annotations

import json
import math
import re
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from .config import DAILY_RESULTS_INBOX, HISTORICAL_RESULTS_INBOX, LEGACY_RESULTS_INBOX
from .db import connect
from .utils import now_iso


def _clean(value):
    if value is None:
        return None
    try:
        if isinstance(value, float) and math.isnan(value):
            return None
    except Exception:
        pass
    text = str(value)
    if text.strip().lower() in {"nan", "none", "null", ""}:
        return None
    return value


def _pos_number(value) -> int | None:
    if value is None:
        return None
    m = re.search(r"\d+", str(value))
    return int(m.group()) if m else None


def _safe_float(value):
    if value is None:
        return None
    text = str(value).replace("£", "").replace(",", "").strip()
    if text in {"", "-", "–", "nan", "None"}:
        return None
    try:
        return float(text)
    except Exception:
        return None


def _read_csv_chunks(path: Path, chunksize: int = 5000):
    return pd.read_csv(path, chunksize=chunksize, low_memory=False)


def _source_files_from_path(path: Path):
    """Yield CSV paths. ZIP files are expanded into a temporary folder."""
    if path.suffix.lower() == ".csv":
        yield path
        return
    if path.suffix.lower() == ".zip":
        with TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            with zipfile.ZipFile(path, "r") as zf:
                zf.extractall(tmpdir)
            for csv_path in sorted(tmpdir.rglob("*.csv")):
                yield csv_path
        return


def _insert_chunk(conn, df: pd.DataFrame, source_name: str) -> int:
    required = {"race_race_id", "race_date", "race_course", "runner_horse"}
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError(f"Results file {source_name} is missing columns: {', '.join(missing)}")

    clean_df = df.astype(object).where(pd.notna(df), None)
    records = clean_df.to_dict("records")

    raw_rows = []
    horse_rows = []
    race_rows = []
    runner_rows = []
    stamp = now_iso()
    for row in records:
        race_id = _clean(row.get("race_race_id"))
        horse_id = _clean(row.get("runner_horse_id")) or _clean(row.get("runner_horse"))
        horse_name = _clean(row.get("runner_horse"))
        if not race_id or not horse_name:
            continue
        compact_keys = [
            "race_race_name", "race_type", "race_class", "race_dist", "race_dist_m", "race_going", "race_surface",
            "race_winning_time_detail", "race_comments", "runner_number", "runner_position", "runner_draw",
            "runner_btn", "runner_ovr_btn", "runner_age", "runner_sex", "runner_weight_lbs", "runner_or",
            "runner_rpr", "runner_tsr", "runner_sp", "runner_sp_dec", "runner_bsp", "runner_prize",
            "runner_jockey", "runner_jockey_id", "runner_trainer", "runner_trainer_id", "runner_owner",
            "runner_sire", "runner_dam", "runner_damsire", "runner_comment", "runner_silk_url"
        ]
        payload_json = json.dumps({k: _clean(row.get(k)) for k in compact_keys if k in row}, ensure_ascii=False, default=str)

        raw_rows.append((
            source_name, race_id, _clean(row.get("race_date")), _clean(row.get("race_course")),
            _clean(row.get("race_off")), _clean(row.get("race_off_dt")), horse_id, horse_name,
            _clean(row.get("runner_position")), _clean(row.get("runner_jockey")), _clean(row.get("runner_trainer")),
            _clean(row.get("runner_silk_url")), payload_json,
        ))
        horse_rows.append((
            horse_id, horse_name, _clean(row.get("runner_silk_url")), _clean(row.get("runner_sire")),
            _clean(row.get("runner_dam")), _clean(row.get("runner_damsire")), _clean(row.get("runner_sex")),
            _clean(row.get("runner_age")), stamp
        ))
        race_rows.append((
            race_id, _clean(row.get("race_date")), _clean(row.get("race_course")), _clean(row.get("race_course_id")),
            _clean(row.get("race_off")), _clean(row.get("race_off_dt")), _clean(row.get("race_race_name")),
            _clean(row.get("race_type")), _clean(row.get("race_class")), _clean(row.get("race_dist")),
            _safe_float(row.get("race_dist_m")), _clean(row.get("race_going")), _clean(row.get("race_surface")),
            _clean(row.get("race_winning_time_detail")), _clean(row.get("race_comments")), payload_json
        ))
        runner_rows.append((
            race_id, horse_id, horse_name, _pos_number(row.get("runner_position")), _clean(row.get("runner_position")),
            _clean(row.get("runner_number")), _clean(row.get("runner_draw")), _safe_float(row.get("runner_btn")),
            _safe_float(row.get("runner_ovr_btn")), _clean(row.get("runner_age")), _clean(row.get("runner_sex")),
            _safe_float(row.get("runner_weight_lbs")), _safe_float(row.get("runner_or")), _safe_float(row.get("runner_rpr")),
            _safe_float(row.get("runner_tsr")), _safe_float(row.get("runner_sp_dec")), _safe_float(row.get("runner_bsp")),
            _safe_float(row.get("runner_prize")), _clean(row.get("runner_jockey_id")), _clean(row.get("runner_jockey")),
            _clean(row.get("runner_trainer_id")), _clean(row.get("runner_trainer")), _clean(row.get("runner_comment")),
            _clean(row.get("runner_silk_url")), payload_json, source_name
        ))

    conn.executemany("""
        INSERT OR IGNORE INTO results_raw (
            source_file, race_race_id, race_date, race_course, race_off, race_off_dt,
            runner_horse_id, runner_horse, runner_position, runner_jockey, runner_trainer,
            runner_silk_url, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, raw_rows)
    conn.executemany("""
        INSERT INTO horses(horse_id, horse_name, silk_url, sire, dam, damsire, sex, latest_age, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(horse_id) DO UPDATE SET
            horse_name=excluded.horse_name,
            silk_url=COALESCE(excluded.silk_url, horses.silk_url),
            sire=COALESCE(excluded.sire, horses.sire),
            dam=COALESCE(excluded.dam, horses.dam),
            damsire=COALESCE(excluded.damsire, horses.damsire),
            sex=COALESCE(excluded.sex, horses.sex),
            latest_age=COALESCE(excluded.latest_age, horses.latest_age),
            updated_at=excluded.updated_at
    """, horse_rows)
    conn.executemany("""
        INSERT OR REPLACE INTO races(race_id, race_date, course, course_id, off_time, off_dt, race_name, race_type, race_class, distance_text, distance_m, going, surface, winning_time, race_comments, payload_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, race_rows)
    conn.executemany("""
        INSERT OR REPLACE INTO result_runners(
            race_id, horse_id, horse_name, finish_position, position_raw, runner_number, draw, btn, ovr_btn,
            age, sex, weight_lbs, official_rating, rpr, tsr, sp_dec, bsp, prize, jockey_id, jockey,
            trainer_id, trainer, comment, silk_url, payload_json, source_file
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, runner_rows)
    return len(raw_rows)


def import_results_file(path: Path, folder_type: str = "daily_results") -> int:
    total = 0
    file_key = f"{folder_type}:{path.name}"
    source_name = file_key
    with connect() as conn:
        already = conn.execute("SELECT 1 FROM processed_source_files WHERE file_key=?", (file_key,)).fetchone()
        if already:
            return 0
        # Backwards compatibility with older versions that only used imported_files.file_name.
        already_old = conn.execute("SELECT 1 FROM imported_files WHERE file_name=?", (file_key,)).fetchone()
        if already_old:
            return 0
        for csv_path in _source_files_from_path(path):
            for chunk in _read_csv_chunks(csv_path):
                total += _insert_chunk(conn, chunk, source_name)
                conn.commit()
        stamp = now_iso()
        conn.execute(
            "INSERT OR REPLACE INTO processed_source_files(file_key, file_name, folder_type, imported_at, row_count) VALUES (?, ?, ?, ?, ?)",
            (file_key, path.name, folder_type, stamp, total),
        )
        conn.execute(
            "INSERT OR REPLACE INTO imported_files(file_name, file_type, imported_at, row_count) VALUES (?, ?, ?, ?)",
            (file_key, folder_type, stamp, total),
        )
    return total


def import_new_results() -> tuple[int, int]:
    """Import historical files first, then daily files into ONE permanent database.

    Important behaviour:
    - The big historical CSV/ZIP is a starting point.
    - Every file in daily_results is appended to the SAME result_runners/races tables.
    - Legacy data_inbox/results is also scanned, so older folder workflows still work.
    - Files already processed are skipped by file key.
    - Duplicate horse/race rows are protected by the result_runners primary key.

    Daily CSV files are not physically appended back into the historical CSV on disk;
    the SQLite database is the growing historical record.
    """
    file_jobs: list[tuple[Path, str]] = []
    seen_paths: set[Path] = set()

    folders = [
        (HISTORICAL_RESULTS_INBOX, "historical_results"),
        (DAILY_RESULTS_INBOX, "daily_results"),
        (LEGACY_RESULTS_INBOX, "daily_results_legacy_results_folder"),
    ]
    for folder, folder_type in folders:
        files = sorted(list(folder.glob("*.csv")) + list(folder.glob("*.zip")))
        for f in files:
            rp = f.resolve()
            if rp in seen_paths:
                continue
            seen_paths.add(rp)
            file_jobs.append((f, folder_type))

    imported_files = 0
    rows = 0
    for path, folder_type in file_jobs:
        count = import_results_file(path, folder_type=folder_type)
        if count:
            imported_files += 1
            rows += count
    return imported_files, rows
