from __future__ import annotations

import json
from collections import defaultdict
from statistics import mean
from typing import Any

from .db import connect
from .identity import normalise_horse_name
from .utils import now_iso


def _rate(num: int, den: int) -> float:
    return round((num / den * 100), 1) if den else 0.0


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).replace("£", "").replace(",", "").strip())
    except Exception:
        return None


def _bucket_distance(distance_m) -> str:
    d = _safe_float(distance_m) or 0
    if d <= 0:
        return "unknown"
    if d <= 1300:
        return "sprint"
    if d <= 1800:
        return "mile-ish"
    if d <= 2400:
        return "middle"
    return "staying"


def _stat_record(rows):
    runs = len(rows)
    wins = sum(1 for r in rows if r.get("finish_position") == 1)
    places = sum(1 for r in rows if r.get("finish_position") and r.get("finish_position") <= 3)
    return {"runs": runs, "wins": wins, "places": places, "strike_rate": _rate(wins, runs), "placing_rate": _rate(places, runs)}


def _canonical_id(horse_id: str | None, horse_name: str | None) -> str:
    if horse_id and str(horse_id).strip():
        return str(horse_id).strip()
    key = normalise_horse_name(horse_name)
    return f"name::{key}" if key else "name::unknown"


def rebuild_horse_feature_cache() -> int:
    """Rebuild the horse intelligence foundation.

    Phase 1 foundation:
    - creates a canonical horse master row for every known horse
    - links source IDs to canonical IDs
    - aggregates all historic result runs into one feature row per horse
    - keeps course/going/distance/rating/recent-run records ready for the API
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT rr.*, r.race_date, r.course, r.off_time, r.off_dt, r.race_name, r.going, r.surface,
                   r.distance_m, r.distance_text
            FROM result_runners rr
            JOIN races r ON r.race_id = rr.race_id
            ORDER BY rr.horse_id, r.race_date, r.off_dt
            """
        ).fetchall()

        by_key: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            d = dict(row)
            key = _canonical_id(d.get("horse_id"), d.get("horse_name"))
            d["canonical_horse_id"] = key
            d["normalised_name"] = normalise_horse_name(d.get("horse_name"))
            by_key[key].append(d)

        now = now_iso()
        master_rows = []
        identity_rows = []
        cache_rows = []
        xp_rows = []

        for canonical_id, horse_rows in by_key.items():
            horse_rows = sorted(horse_rows, key=lambda r: (r.get("race_date") or "", r.get("off_dt") or ""))
            first = horse_rows[0]
            latest = horse_rows[-1]
            name = latest.get("horse_name") or first.get("horse_name") or canonical_id
            norm = normalise_horse_name(name)
            silk = next((r.get("silk_url") for r in reversed(horse_rows) if r.get("silk_url")), None)
            payload_latest = {}
            try:
                payload_latest = json.loads(latest.get("payload_json") or "{}")
            except Exception:
                pass

            master_rows.append((
                canonical_id, name, norm, silk,
                payload_latest.get("runner_sire"), payload_latest.get("runner_dam"), payload_latest.get("runner_damsire"),
                latest.get("sex") or payload_latest.get("runner_sex"), latest.get("age") or payload_latest.get("runner_age"),
                first.get("race_date"), latest.get("race_date"), now,
            ))
            source_ids = {r.get("horse_id") for r in horse_rows if r.get("horse_id")}
            for source_id in source_ids:
                identity_rows.append((source_id, canonical_id, name, norm, now))

            runs = len(horse_rows)
            wins = sum(1 for r in horse_rows if r.get("finish_position") == 1)
            places = sum(1 for r in horse_rows if r.get("finish_position") and r.get("finish_position") <= 3)
            prize = sum(float(r.get("prize") or 0) for r in horse_rows)
            rprs = [float(r["rpr"]) for r in horse_rows if r.get("rpr") is not None]
            avg3 = round(mean(rprs[-3:]), 1) if rprs else None
            avg5 = round(mean(rprs[-5:]), 1) if rprs else None
            trend = None
            if len(rprs) >= 6:
                trend = round(mean(rprs[-3:]) - mean(rprs[-6:-3]), 1)
            elif len(rprs) >= 3:
                trend = round(rprs[-1] - rprs[0], 1)

            course_groups = defaultdict(list)
            going_groups = defaultdict(list)
            distance_groups = defaultdict(list)
            for r in horse_rows:
                course_groups[r.get("course") or "Unknown"].append(r)
                going_groups[r.get("going") or "Unknown"].append(r)
                distance_groups[_bucket_distance(r.get("distance_m"))].append(r)

            recent_runs = []
            rating_points = []
            for r in reversed(horse_rows[-20:]):
                recent_runs.append({
                    "date": r.get("race_date"), "course": r.get("course"), "time": r.get("off_time"),
                    "race_id": r.get("race_id"), "race_name": r.get("race_name"), "position": r.get("position_raw"),
                    "rpr": r.get("rpr"), "or": r.get("official_rating"), "tsr": r.get("tsr"), "sp": r.get("sp_dec"),
                    "beaten_distance": r.get("ovr_btn") if r.get("ovr_btn") is not None else r.get("btn"),
                    "comment": r.get("comment"), "prize": r.get("prize"),
                })
            for r in horse_rows:
                if r.get("rpr") is not None or r.get("official_rating") is not None:
                    rating_points.append({
                        "date": r.get("race_date"), "course": r.get("course"), "race_name": r.get("race_name"),
                        "rpr": r.get("rpr"), "official": r.get("official_rating"), "tsr": r.get("tsr"),
                    })

            cache_rows.append((
                canonical_id, name, runs, wins, places, round(prize, 2),
                _rate(wins, runs), _rate(places, runs), max(rprs) if rprs else None,
                rprs[-1] if rprs else None, avg3, avg5, trend, latest.get("race_date"),
                latest.get("trainer"), latest.get("jockey"),
                json.dumps({k: _stat_record(v) for k, v in course_groups.items()}, ensure_ascii=False),
                json.dumps({k: _stat_record(v) for k, v in going_groups.items()}, ensure_ascii=False),
                json.dumps({k: _stat_record(v) for k, v in distance_groups.items()}, ensure_ascii=False),
                json.dumps(recent_runs, ensure_ascii=False), now,
            ))

        conn.execute("DELETE FROM horse_master")
        conn.execute("DELETE FROM horse_identity_map")
        conn.execute("DELETE FROM horse_feature_cache")
        conn.executemany(
            """
            INSERT INTO horse_master(
                canonical_horse_id, horse_name, normalised_name, latest_silk_url, sire, dam, damsire, sex,
                latest_age, first_seen_date, last_seen_date, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            master_rows,
        )
        conn.executemany(
            """
            INSERT OR REPLACE INTO horse_identity_map(source_horse_id, canonical_horse_id, horse_name, normalised_name, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            identity_rows,
        )
        conn.executemany(
            """
            INSERT INTO horse_feature_cache(
                horse_id, horse_name, runs, wins, places, prize_money, strike_rate, placing_rate,
                best_rpr, latest_rpr, avg_rpr_last3, avg_rpr_last5, rpr_trend, last_run_date,
                latest_trainer, latest_jockey, course_stats_json, going_stats_json, distance_stats_json,
                recent_runs_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            cache_rows,
        )
        return len(cache_rows)
