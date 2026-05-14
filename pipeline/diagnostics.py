from __future__ import annotations

from pathlib import Path
from .config import HISTORICAL_RESULTS_INBOX, DAILY_RESULTS_INBOX, DAILY_RACECARDS_INBOX, LEGACY_RESULTS_INBOX, LEGACY_RACECARDS_INBOX, DB_PATH
from .db import connect, init_db


def _folder_files(folder: Path):
    return [p.name for p in sorted(list(folder.glob('*.csv')) + list(folder.glob('*.zip')))]


def build_diagnostics() -> dict:
    init_db()
    with connect() as conn:
        def scalar(sql, params=()):
            return conn.execute(sql, params).fetchone()[0]
        processed = [dict(r) for r in conn.execute(
            'SELECT file_name, folder_type, imported_at, row_count FROM processed_source_files ORDER BY imported_at DESC LIMIT 20'
        ).fetchall()]
        latest_results = conn.execute('SELECT MAX(race_date) AS d FROM results_raw').fetchone()['d']
        latest_cards = conn.execute('SELECT MAX(race_date) AS d FROM racecards_raw').fetchone()['d']
        return {
            'database': str(DB_PATH),
            'folders': {
                'historical_results': str(HISTORICAL_RESULTS_INBOX),
                'daily_results': str(DAILY_RESULTS_INBOX),
                'daily_racecards': str(DAILY_RACECARDS_INBOX),
                'legacy_results_still_scanned': str(LEGACY_RESULTS_INBOX),
                'legacy_racecards_still_scanned': str(LEGACY_RACECARDS_INBOX),
            },
            'files_seen_in_folders': {
                'historical_results': _folder_files(HISTORICAL_RESULTS_INBOX),
                'daily_results': _folder_files(DAILY_RESULTS_INBOX),
                'daily_racecards': _folder_files(DAILY_RACECARDS_INBOX),
                'legacy_results': _folder_files(LEGACY_RESULTS_INBOX),
                'legacy_racecards': _folder_files(LEGACY_RACECARDS_INBOX),
            },
            'database_counts': {
                'raw_result_rows': scalar('SELECT COUNT(*) FROM results_raw'),
                'normalised_result_runners': scalar('SELECT COUNT(*) FROM result_runners'),
                'result_races': scalar('SELECT COUNT(*) FROM races'),
                'horse_profiles': scalar('SELECT COUNT(*) FROM horse_feature_cache'),
                'racecard_rows': scalar('SELECT COUNT(*) FROM racecards_raw'),
                'latest_result_date': latest_results,
                'latest_racecard_date': latest_cards,
            },
            'recent_processed_files': processed,
            'healthy': scalar('SELECT COUNT(*) FROM horse_feature_cache') > 0 and scalar('SELECT COUNT(*) FROM result_runners') > 0,
            'hint': 'Daily results are permanently merged into the main database. If you drop files into old data_inbox/results or racecards folders, they are still scanned. If counts look wrong, click Rebuild Historical Database to clear old import flags and reload every CSV/ZIP.'
        }
