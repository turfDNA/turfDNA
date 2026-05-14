from __future__ import annotations

import traceback
from pathlib import Path

from .build_daily_outputs import build_all_outputs
from .config import DB_PATH, RACECARDS_INBOX, RESULTS_INBOX, LOGS_DIR
from .db import connect, init_db
from .import_racecards import import_new_racecards
from .import_results import import_new_results
from .feature_builder import rebuild_horse_feature_cache
from .utils import now_iso


def rebuild_database_from_folders() -> dict:
    """Clear imported result/racecard tables and rebuild from files in data_inbox.

    Use this when a historical file was added after the first run, or when a file
    was skipped because an older version had already marked it as processed.
    """
    init_db()
    with connect() as conn:
        for table in [
            "results_raw", "result_runners", "races", "racecards_raw",
            "horse_feature_cache", "horse_master", "horse_identity_map", "horse_xp_history",
            "processed_source_files", "imported_files"
        ]:
            conn.execute(f"DELETE FROM {table}")
    return run_daily_pipeline()


def suggested_fix_for_error(error: Exception) -> str:
    text = str(error).lower()
    if "missing columns" in text:
        return "Check the CSV is the Racing Post export format and has not been edited or saved with missing headings."
    if "permission" in text:
        return "Close the CSV or database file if it is open in Excel, then run again."
    if "no such table" in text:
        return "The database was not created correctly. Run the pipeline again; it creates missing tables automatically."
    return "Check the uploaded CSV files, then try again. If it repeats, save the error message and review the file named in the error."


def run_daily_pipeline() -> dict:
    init_db()
    started = now_iso()
    run_id = None
    summary = {
        "run_started_at": started,
        "status": "running",
        "database": str(DB_PATH),
        "results_folder": str(RESULTS_INBOX),
        "racecards_folder": str(RACECARDS_INBOX),
    }

    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO daily_run_log(run_started_at, status, message) VALUES (?, 'running', 'Pipeline started')",
            (started,),
        )
        run_id = cur.lastrowid

    try:
        results_files, result_rows = import_new_results()
        profiles_rebuilt = rebuild_horse_feature_cache()
        racecard_files, racecard_rows = import_new_racecards()
        build_all_outputs()

        with connect() as conn:
            races_found = conn.execute("SELECT COUNT(DISTINCT race_race_id) AS n FROM racecards_raw").fetchone()["n"]
            runners_found = conn.execute("SELECT COUNT(*) AS n FROM racecards_raw WHERE is_non_runner=0").fetchone()["n"]
            total_result_runners = conn.execute("SELECT COUNT(*) AS n FROM result_runners").fetchone()["n"]
            total_result_races = conn.execute("SELECT COUNT(*) AS n FROM races").fetchone()["n"]
            latest_result_date = conn.execute("SELECT MAX(race_date) AS d FROM races").fetchone()["d"]
            finished = now_iso()
            message = "Daily update completed"
            conn.execute(
                """
                UPDATE daily_run_log
                SET run_finished_at=?, status='success', message=?, results_files=?, racecard_files=?,
                    result_rows=?, racecard_rows=?, races_found=?, runners_found=?, suggested_fix=NULL
                WHERE id=?
                """,
                (finished, message, results_files, racecard_files, result_rows, racecard_rows, races_found, runners_found, run_id),
            )
        summary.update({
            "run_finished_at": finished,
            "status": "success",
            "message": message,
            "results_files_imported": results_files,
            "racecard_files_imported": racecard_files,
            "result_rows_imported": result_rows,
            "racecard_rows_imported": racecard_rows,
            "races_in_database": races_found,
            "active_runners_in_database": runners_found,
            "total_result_runners_in_main_database": total_result_runners,
            "total_result_races_in_main_database": total_result_races,
            "latest_result_date_in_main_database": latest_result_date,
            "horse_profiles_rebuilt": profiles_rebuilt,
        })
        build_all_outputs(summary)
        return summary
    except Exception as exc:
        finished = now_iso()
        fix = suggested_fix_for_error(exc)
        error_text = f"{type(exc).__name__}: {exc}"
        trace_path = LOGS_DIR / "last_error.txt"
        trace_path.write_text(traceback.format_exc(), encoding="utf-8")
        with connect() as conn:
            conn.execute(
                """
                UPDATE daily_run_log
                SET run_finished_at=?, status='error', message=?, suggested_fix=?
                WHERE id=?
                """,
                (finished, error_text, fix, run_id),
            )
        summary.update({
            "run_finished_at": finished,
            "status": "error",
            "message": error_text,
            "suggested_fix": fix,
        })
        build_all_outputs(summary)
        return summary


if __name__ == "__main__":
    import json
    print(json.dumps(run_daily_pipeline(), indent=2))
