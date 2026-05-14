from __future__ import annotations

import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from .config import DAILY_RACECARDS_INBOX, LEGACY_RACECARDS_INBOX
from .db import connect
from .utils import now_iso, read_csv, row_payload


def is_non_runner(row) -> int:
    values = [
        row.get("runner_jockey"),
        row.get("runner_medical"),
        row.get("race_race_status"),
        row.get("runner_number"),
    ]
    joined = " ".join(str(v or "") for v in values).upper()
    if "NON-RUNNER" in joined or joined.strip() == "NR":
        return 1
    if str(row.get("runner_jockey") or "").upper() == "NON-RUNNER":
        return 1
    return 0


def _source_files_from_path(path: Path):
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


def import_racecard_file(path: Path, folder_type: str = "daily_racecards") -> int:
    file_key = f"{folder_type}:{path.name}"
    total = 0
    with connect() as conn:
        already = conn.execute("SELECT 1 FROM imported_files WHERE file_name=?", (file_key,)).fetchone()
        if already:
            return 0
        for csv_path in _source_files_from_path(path):
            df = read_csv(csv_path)
            required = {"race_race_id", "race_date", "race_course", "runner_horse"}
            missing = sorted(required - set(df.columns))
            if missing:
                raise ValueError(f"Racecard file {path.name} is missing columns: {', '.join(missing)}")
            rows = []
            for _, row in df.iterrows():
                rows.append((
                    file_key,
                    row.get("race_race_id"),
                    row.get("race_date"),
                    row.get("race_course"),
                    row.get("race_off_time") or row.get("race_off"),
                    row.get("race_off_dt"),
                    row.get("runner_horse_id"),
                    row.get("runner_horse"),
                    row.get("runner_jockey"),
                    row.get("runner_trainer"),
                    row.get("runner_silk_url"),
                    is_non_runner(row),
                    row_payload(row),
                ))
            conn.executemany(
                """
                INSERT OR IGNORE INTO racecards_raw (
                    source_file, race_race_id, race_date, race_course, race_off_time, race_off_dt,
                    runner_horse_id, runner_horse, runner_jockey, runner_trainer, runner_silk_url,
                    is_non_runner, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            total += len(df)
        conn.execute(
            "INSERT INTO imported_files(file_name, file_type, imported_at, row_count) VALUES (?, ?, ?, ?)",
            (file_key, folder_type, now_iso(), total),
        )
    return total


def import_new_racecards() -> tuple[int, int]:
    file_jobs: list[tuple[Path, str]] = []
    seen_paths: set[Path] = set()
    for folder, folder_type in [
        (DAILY_RACECARDS_INBOX, "daily_racecards"),
        (LEGACY_RACECARDS_INBOX, "daily_racecards_legacy_racecards_folder"),
    ]:
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
        count = import_racecard_file(path, folder_type=folder_type)
        if count:
            imported_files += 1
            rows += count
    return imported_files, rows
