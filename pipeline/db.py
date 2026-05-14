from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .config import DB_PATH


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS imported_files (
                file_name TEXT PRIMARY KEY,
                file_type TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                row_count INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS processed_source_files (
                file_key TEXT PRIMARY KEY,
                file_name TEXT NOT NULL,
                folder_type TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                row_count INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS results_raw (
                source_file TEXT NOT NULL,
                race_race_id TEXT,
                race_date TEXT,
                race_course TEXT,
                race_off TEXT,
                race_off_dt TEXT,
                runner_horse_id TEXT,
                runner_horse TEXT,
                runner_position TEXT,
                runner_jockey TEXT,
                runner_trainer TEXT,
                runner_silk_url TEXT,
                payload_json TEXT NOT NULL,
                UNIQUE(source_file, race_race_id, runner_horse_id, runner_horse)
            );

            CREATE TABLE IF NOT EXISTS racecards_raw (
                source_file TEXT NOT NULL,
                race_race_id TEXT,
                race_date TEXT,
                race_course TEXT,
                race_off_time TEXT,
                race_off_dt TEXT,
                runner_horse_id TEXT,
                runner_horse TEXT,
                runner_jockey TEXT,
                runner_trainer TEXT,
                runner_silk_url TEXT,
                is_non_runner INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL,
                UNIQUE(source_file, race_race_id, runner_horse_id, runner_horse)
            );

            CREATE TABLE IF NOT EXISTS horses (
                horse_id TEXT PRIMARY KEY,
                horse_name TEXT NOT NULL,
                silk_url TEXT,
                sire TEXT,
                dam TEXT,
                damsire TEXT,
                sex TEXT,
                latest_age TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS races (
                race_id TEXT PRIMARY KEY,
                race_date TEXT,
                course TEXT,
                course_id TEXT,
                off_time TEXT,
                off_dt TEXT,
                race_name TEXT,
                race_type TEXT,
                race_class TEXT,
                distance_text TEXT,
                distance_m REAL,
                going TEXT,
                surface TEXT,
                winning_time TEXT,
                race_comments TEXT,
                payload_json TEXT
            );

            CREATE TABLE IF NOT EXISTS result_runners (
                race_id TEXT NOT NULL,
                horse_id TEXT NOT NULL,
                horse_name TEXT NOT NULL,
                finish_position INTEGER,
                position_raw TEXT,
                runner_number TEXT,
                draw TEXT,
                btn REAL,
                ovr_btn REAL,
                age TEXT,
                sex TEXT,
                weight_lbs REAL,
                official_rating REAL,
                rpr REAL,
                tsr REAL,
                sp_dec REAL,
                bsp REAL,
                prize REAL,
                jockey_id TEXT,
                jockey TEXT,
                trainer_id TEXT,
                trainer TEXT,
                comment TEXT,
                silk_url TEXT,
                payload_json TEXT,
                source_file TEXT,
                PRIMARY KEY (race_id, horse_id)
            );

            CREATE TABLE IF NOT EXISTS horse_feature_cache (
                horse_id TEXT PRIMARY KEY,
                horse_name TEXT,
                runs INTEGER,
                wins INTEGER,
                places INTEGER,
                prize_money REAL,
                strike_rate REAL,
                placing_rate REAL,
                best_rpr REAL,
                latest_rpr REAL,
                avg_rpr_last3 REAL,
                avg_rpr_last5 REAL,
                rpr_trend REAL,
                last_run_date TEXT,
                latest_trainer TEXT,
                latest_jockey TEXT,
                course_stats_json TEXT,
                going_stats_json TEXT,
                distance_stats_json TEXT,
                recent_runs_json TEXT,
                updated_at TEXT
            );



            CREATE TABLE IF NOT EXISTS horse_master (
                canonical_horse_id TEXT PRIMARY KEY,
                horse_name TEXT NOT NULL,
                normalised_name TEXT NOT NULL,
                latest_silk_url TEXT,
                sire TEXT,
                dam TEXT,
                damsire TEXT,
                sex TEXT,
                latest_age TEXT,
                first_seen_date TEXT,
                last_seen_date TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS horse_identity_map (
                source_horse_id TEXT PRIMARY KEY,
                canonical_horse_id TEXT NOT NULL,
                horse_name TEXT,
                normalised_name TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS horse_xp_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_horse_id TEXT NOT NULL,
                race_id TEXT,
                race_date TEXT,
                course TEXT,
                race_name TEXT,
                xp REAL,
                xp_type TEXT NOT NULL,
                breakdown_json TEXT,
                created_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_horse_master_norm ON horse_master(normalised_name);
            CREATE INDEX IF NOT EXISTS idx_identity_canonical ON horse_identity_map(canonical_horse_id);
            CREATE INDEX IF NOT EXISTS idx_xp_history_horse ON horse_xp_history(canonical_horse_id, race_date);

            CREATE TABLE IF NOT EXISTS daily_run_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_started_at TEXT NOT NULL,
                run_finished_at TEXT,
                status TEXT NOT NULL,
                message TEXT,
                results_files INTEGER DEFAULT 0,
                racecard_files INTEGER DEFAULT 0,
                result_rows INTEGER DEFAULT 0,
                racecard_rows INTEGER DEFAULT 0,
                races_found INTEGER DEFAULT 0,
                runners_found INTEGER DEFAULT 0,
                suggested_fix TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_results_raw_horse ON results_raw(runner_horse_id, race_date);
            CREATE INDEX IF NOT EXISTS idx_result_runners_horse ON result_runners(horse_id, race_id);
            CREATE INDEX IF NOT EXISTS idx_races_date_course ON races(race_date, course, off_dt);
            CREATE INDEX IF NOT EXISTS idx_racecards_raw_date ON racecards_raw(race_date, race_course, race_off_dt);
            """
        )
