from __future__ import annotations

import json
import re
from datetime import datetime

from .config import OUTPUTS_DIR
from .db import connect
from .build_suggestions import build_suggestions_for_latest_racecard, expected_performance_score, expected_performance_breakdown, _load_profiles, _profile_for_payload
from .utils import safe_float
from .race_intelligence import build_race_intelligence



from datetime import datetime


def _parse_date_value(value):
    """Parse dates safely so latest means newest calendar date, not first text value."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return None
    # Common formats seen in Racing Post exports and Excel-resaved files.
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except Exception:
            pass
    try:
        return datetime.fromisoformat(text[:10]).date()
    except Exception:
        return None


def _latest_race_date(conn, table_name: str) -> str | None:
    """Return the actual newest race_date stored in a table.

    SQLite MAX(text) is only safe when every date is perfectly ISO formatted.
    This Python check prevents old dates being selected if the CSV/date format changes.
    """
    rows = conn.execute(f"SELECT DISTINCT race_date FROM {table_name} WHERE race_date IS NOT NULL").fetchall()
    best_raw = None
    best_date = None
    for row in rows:
        raw = row["race_date"]
        parsed = _parse_date_value(raw)
        if parsed is None:
            continue
        if best_date is None or parsed > best_date:
            best_date = parsed
            best_raw = raw
    return best_raw


def _payload(row) -> dict:
    return json.loads(row["payload_json"])


def _write_json(name: str, data) -> None:
    path = OUTPUTS_DIR / name
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _pos_number(value) -> int | None:
    if value is None:
        return None
    m = re.search(r"\d+", str(value))
    return int(m.group()) if m else None



def _runner_number_key(value) -> tuple[int, int | str]:
    """Natural runner-number ordering for racecards.

    CSV values often arrive as text, so plain sorting puts 10 before 2.
    Numeric runner numbers come first in real numeric order. Non-numeric
    labels such as SU, NR, RES, blank, etc. are pushed to the bottom.
    """
    if value is None:
        return (1, "")
    text = str(value).strip().upper()
    if not text or text in {"NAN", "NONE", "NULL"}:
        return (1, "")
    match = re.search(r"\d+", text)
    if match:
        return (0, int(match.group()))
    return (1, text)

def _run_merit_comment_parts(payload: dict) -> list[dict]:
    """Comment-based run-merit adjustments.

    XP should measure the quality of the run, not just finishing position.
    These simple rules reward horses that ran better than the bare result and
    penalise horses that had ideal runs but weakened, stopped or were eased.
    """
    comment = _clean_text(payload.get("runner_comment") or payload.get("comment")).lower()
    if not comment:
        return []
    checks = [
        (["hampered", "short of room", "not clear run", "denied clear run", "blocked", "checked", "interference"], "Trouble in running", 7.0),
        (["slowly away", "dwelt", "missed break", "awkward start", "reared start"], "Slow start / lost position", 5.0),
        (["wide", "raced wide", "carried wide"], "Covered extra ground", 3.5),
        (["stayed on", "ran on", "finished well", "late headway", "kept on well"], "Strong late finish", 5.0),
        (["outpaced", "never nearer"], "Ran on after being outpaced", 2.5),
        (["made all", "always doing enough", "readily", "comfortably", "easily"], "Efficient winning performance", 4.0),
        (["weakened", "faded", "no extra", "tired", "lost place"], "Weakened late", -5.0),
        (["eased", "tailed off", "pulled up", "struggling"], "Low finishing effort", -8.0),
        (["mistake", "blundered", "jumped left", "jumped right"], "Jumping/fluency issue", 2.0),
    ]
    parts=[]
    for needles, label, value in checks:
        if any(n in comment for n in needles):
            parts.append({"label": label, "value": value})
    total=sum(p["value"] for p in parts)
    if total > 14:
        scale=14/total
        parts=[{"label": p["label"], "value": round(p["value"]*scale,1)} for p in parts]
    if total < -12:
        scale=-12/total
        parts=[{"label": p["label"], "value": round(p["value"]*scale,1)} for p in parts]
    return parts


def result_run_score(payload: dict) -> float:
    """XP v2 / Run Merit Score for completed results.

    This now scores how well the horse ran, not simply where it finished.
    A troubled fourth can outscore a perfectly placed second when the data says
    the run contained more merit.
    """
    return round(max(0.0, min(100.0, sum(p["value"] for p in result_score_breakdown(payload)["parts"]))), 1)


def result_score_breakdown(payload: dict) -> dict:
    parts = [{"label": "Starting point", "value": 50.0}]
    pos = _pos_number(payload.get("runner_position"))
    if pos:
        if pos == 1: v = 25
        elif pos == 2: v = 18
        elif pos == 3: v = 13
        elif pos == 4: v = 9
        elif pos == 5: v = 6
        elif pos == 6: v = 4
        else: v = max(0, 8 - pos)
        parts.append({"label": f"Finishing position {pos}", "value": round(v, 1)})
    beaten = safe_float(payload.get("runner_ovr_btn") or payload.get("runner_btn"))
    if beaten is not None:
        penalty = -min(22.0, beaten * 1.8)
        if beaten <= 1.0 and pos and pos > 1:
            penalty += 3.0
        parts.append({"label": "Beaten distance / closeness", "value": round(penalty, 1)})
    rpr = safe_float(payload.get("runner_rpr"))
    if rpr:
        parts.append({"label": "RPR rating", "value": round(max(-8.0, min(18.0, (rpr - 70.0) * 0.20)), 1)})
    tsr = safe_float(payload.get("runner_tsr"))
    if tsr:
        parts.append({"label": "TS rating", "value": round(max(-6.0, min(10.0, (tsr - 60.0) * 0.12)), 1)})
    odds = safe_float(payload.get("runner_sp_dec"))
    if odds and pos:
        if pos <= 3 and odds >= 8:
            parts.append({"label": "Beat market expectation", "value": 4.0})
        elif pos > 5 and odds <= 4:
            parts.append({"label": "Below market expectation", "value": -5.0})
        elif pos >= 4 and odds >= 12 and beaten is not None and beaten <= 3:
            parts.append({"label": "Outran big price", "value": 3.0})
    parts.extend(_run_merit_comment_parts(payload))
    total = round(max(0.0, min(100.0, sum(p["value"] for p in parts))), 1)
    return {"total": total, "parts": parts, "xp_version": "XP v2 Run Merit"}

def _clean_text(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "-"}:
        return ""
    return text

def _fmt(value, suffix="") -> str:
    text = _clean_text(value)
    return f"{text}{suffix}" if text else "-"

def _comment_signals(comment: str) -> list[str]:
    """Extract simple story signals from Racing Post-style runner comments."""
    c = _clean_text(comment).lower()
    signals = []
    checks = [
        ("made all", "made all / led throughout"),
        ("led", "led or raced prominently"),
        ("prominent", "raced prominently"),
        ("towards rear", "came from the rear"),
        ("held up", "held up"),
        ("headway", "made headway"),
        ("stayed on", "stayed on late"),
        ("kept on", "kept on"),
        ("weakened", "weakened late"),
        ("outpaced", "outpaced"),
        ("hampered", "hampered"),
        ("short of room", "short of room"),
        ("not clear run", "not clear run"),
        ("mistake", "made a mistake"),
        ("jumped", "jumping noted"),
        ("ran on", "ran on"),
    ]
    for needle, label in checks:
        if needle in c and label not in signals:
            signals.append(label)
    return signals[:4]

def _race_story(race: dict) -> dict:
    """Build a readable race story from result fields and runner comments."""
    runners = sorted(race.get("runners", []), key=lambda r: _pos_number(r.get("position")) or 999)
    if not runners:
        return {"headline": "Race story not available yet.", "bullets": [], "key_runners": [], "pace_clues": [], "incidents": [], "data_points": []}

    winner = runners[0]
    second = runners[1] if len(runners) > 1 else None
    third = runners[2] if len(runners) > 2 else None
    field_size = len(runners)
    margin = _clean_text(second.get("beaten_distance") if second else "")
    winning_time = _clean_text(race.get("winning_time"))

    headline = f"{winner.get('horse', 'The winner')} won"
    if margin:
        headline += f" by {margin} lengths"
    if winning_time:
        headline += f" in {winning_time}"
    headline += "."

    winner_comment = _clean_text(winner.get("comment"))
    runner_comments = [(r, _clean_text(r.get("comment"))) for r in runners if _clean_text(r.get("comment"))]

    bullets = []
    if winner_comment:
        bullets.append(f"Winner: {winner_comment}")
    if second and _clean_text(second.get("comment")):
        bullets.append(f"Second: {_clean_text(second.get('comment'))}")
    if third and _clean_text(third.get("comment")):
        bullets.append(f"Third: {_clean_text(third.get('comment'))}")

    # Pace/read of the race from comments.
    pace_signals = []
    for r, comment in runner_comments:
        for sig in _comment_signals(comment):
            if sig not in pace_signals:
                pace_signals.append(sig)
    pace_clues = pace_signals[:8]

    incidents = []
    incident_words = ["hampered", "short of room", "not clear run", "mistake", "unseated", "fell", "stumbled", "interference", "checked"]
    for r, comment in runner_comments:
        lc = comment.lower()
        if any(w in lc for w in incident_words):
            incidents.append({"horse": r.get("horse"), "note": comment})
        if len(incidents) >= 5:
            break

    key_runners = []
    for r in runners[:5]:
        key_runners.append({
            "position": r.get("position"),
            "horse": r.get("horse"),
            "sp": r.get("sp"),
            "rpr": r.get("rpr"),
            "beaten_distance": r.get("beaten_distance"),
            "comment": _clean_text(r.get("comment")),
            "run_score": r.get("run_score") or r.get("xp"),
        })

    data_points = [
        {"label": "Field size", "value": field_size},
        {"label": "Winning SP", "value": winner.get("sp") or "-"},
        {"label": "Winning RPR", "value": winner.get("rpr") or "-"},
        {"label": "Winning margin", "value": margin or "-"},
        {"label": "Winning time", "value": winning_time or "-"},
    ]

    if not bullets:
        bullets.append("Detailed runner comments were not available for this race, so the story is based on finishing order, margin, SP and ratings.")

    return {
        "headline": headline,
        "bullets": bullets[:5],
        "key_runners": key_runners,
        "pace_clues": pace_clues,
        "incidents": incidents,
        "data_points": data_points,
    }

def _race_summary(race: dict) -> dict:
    runners = sorted(race["runners"], key=lambda r: _pos_number(r.get("position")) or 999)
    story = _race_story(race)
    winner = runners[0] if runners else {}
    return {"winner": winner, "top_3": runners[:3], "summary": story.get("headline"), "story": story}


def build_latest_results_review() -> dict:
    with connect() as conn:
        latest = _latest_race_date(conn, "results_raw")
        if not latest:
            data = {"date": None, "courses": []}
            _write_json("latest_results_review.json", data)
            return data
        rows = conn.execute(
            "SELECT * FROM results_raw WHERE race_date=? ORDER BY race_course, race_off_dt, race_off",
            (latest,),
        ).fetchall()

    grouped: dict[str, dict] = {}
    for row in rows:
        payload = _payload(row)
        course = row["race_course"] or "Unknown course"
        race_id = row["race_race_id"] or f'{course}-{row["race_off"]}'
        grouped.setdefault(course, {"course": course, "races": {}})
        race = grouped[course]["races"].setdefault(race_id, {
            "race_id": race_id,
            "time": row["race_off"],
            "race_name": payload.get("race_race_name"),
            "going": payload.get("race_going"),
            "distance": payload.get("race_dist") or payload.get("race_distance"),
            "winning_time": payload.get("race_winning_time_detail"),
            "race_comments": payload.get("race_comments"),
            "runners": [],
        })
        race["runners"].append({
            "position": row["runner_position"],
            "horse_id": row["runner_horse_id"],
            "horse": row["runner_horse"],
            "jockey": row["runner_jockey"],
            "trainer": row["runner_trainer"],
            "silk_url": row["runner_silk_url"],
            "sp": payload.get("runner_sp") or payload.get("runner_sp_dec"),
            "beaten_distance": payload.get("runner_ovr_btn") or payload.get("runner_btn"),
            "rpr": payload.get("runner_rpr"),
            "tsr": payload.get("runner_tsr"),
            "comment": payload.get("runner_comment"),
            "run_score": result_run_score(payload),
            "xp": result_run_score(payload),
            "xp_breakdown": result_score_breakdown(payload),
        })

    courses = []
    for course_data in grouped.values():
        races = list(course_data["races"].values())
        for race in races:
            race["runners"] = sorted(race["runners"], key=lambda r: _pos_number(r.get("position")) or 999)
            race["result_review"] = _race_summary(race)
        courses.append({"course": course_data["course"], "races": sorted(races, key=lambda r: r.get("time") or "")})

    data = {"date": latest, "courses": sorted(courses, key=lambda c: c["course"])}
    _write_json("latest_results_review.json", data)
    return data


def build_latest_racecards() -> dict:
    suggestions = build_suggestions_for_latest_racecard()
    profiles = _load_profiles()
    suggestion_by_race = {r["race_id"]: r for r in suggestions}

    with connect() as conn:
        latest = _latest_race_date(conn, "racecards_raw")
        if not latest:
            data = {"date": None, "courses": []}
            _write_json("latest_racecards.json", data)
            return data
        rows = conn.execute(
            "SELECT * FROM racecards_raw WHERE race_date=? AND is_non_runner=0 ORDER BY race_course, race_off_dt, race_off_time",
            (latest,),
        ).fetchall()

    grouped = {}
    for row in rows:
        payload = _payload(row)
        course = row["race_course"] or "Unknown course"
        race_id = row["race_race_id"] or f'{course}-{row["race_off_time"]}'
        suggestion = suggestion_by_race.get(race_id, {})
        grouped.setdefault(course, {"course": course, "races": {}})
        grouped[course]["races"].setdefault(race_id, {
            "race_id": race_id,
            "time": row["race_off_time"],
            "race_name": payload.get("race_race_name"),
            "going": payload.get("race_going"),
            "distance": payload.get("race_distance") or payload.get("race_distance_round"),
            "surface": payload.get("race_surface"),
            "field_size": payload.get("race_field_size"),
            "suggested_1_2_3": suggestion.get("suggested_1_2_3", []),
            "runners": [],
        })
        profile = _profile_for_payload(payload, profiles)
        breakdown = expected_performance_breakdown(payload, profile)
        xp = breakdown["total"]
        grouped[course]["races"][race_id]["runners"].append({
            "horse_id": row["runner_horse_id"],
            "horse": row["runner_horse"],
            "jockey": row["runner_jockey"],
            "trainer": row["runner_trainer"],
            "silk_url": row["runner_silk_url"],
            "number": payload.get("runner_number"),
            "draw": payload.get("runner_draw"),
            "odds_decimal": payload.get("runner_odds_decimal"),
            "odds_fractional": payload.get("runner_odds_fractional"),
            "odds_bookmaker": payload.get("runner_odds_bookmaker"),
            "odds_updated": payload.get("runner_odds_updated"),
            "form": payload.get("runner_form"),
            "rpr": payload.get("runner_rpr"),
            "ofr": payload.get("runner_ofr") or payload.get("runner_or"),
            "xp": xp,
            "score": xp,
            "xp_breakdown": breakdown,
            "confidence": breakdown.get("confidence"),
        })

    courses = []
    for course_data in grouped.values():
        races = list(course_data["races"].values())
        for race in races:
            # Keep the full runner list in racecard/programme order, not text order and not XP order.
            # The data suggested 1-2-3 remains separately ranked by XP.
            race["runners"] = sorted(
                race["runners"],
                key=lambda r: (_runner_number_key(r.get("number")), str(r.get("horse") or "")),
            )
            race["race_intelligence"] = build_race_intelligence(
                course_data["course"],
                race.get("distance"),
                race.get("going"),
                race.get("surface"),
                race.get("runners", []),
            )
        courses.append({"course": course_data["course"], "races": sorted(races, key=lambda r: r.get("time") or "")})

    data = {"date": latest, "courses": sorted(courses, key=lambda c: c["course"])}
    _write_json("latest_racecards.json", data)
    _write_json("latest_suggestions.json", suggestions)
    return data


def build_admin_status(run_summary: dict | None = None) -> dict:
    with connect() as conn:
        latest_run = conn.execute("SELECT * FROM daily_run_log ORDER BY id DESC LIMIT 1").fetchone()
        imported = conn.execute("SELECT file_type, COUNT(*) AS files, SUM(row_count) AS rows FROM imported_files GROUP BY file_type").fetchall()
        totals = {
            "main_result_runners": conn.execute("SELECT COUNT(*) AS n FROM result_runners").fetchone()["n"],
            "main_result_races": conn.execute("SELECT COUNT(*) AS n FROM races").fetchone()["n"],
            "raw_result_rows": conn.execute("SELECT COUNT(*) AS n FROM results_raw").fetchone()["n"],
            "racecard_rows": conn.execute("SELECT COUNT(*) AS n FROM racecards_raw").fetchone()["n"],
            "horse_profiles": conn.execute("SELECT COUNT(*) AS n FROM horse_feature_cache").fetchone()["n"],
        }
    data = {
        "latest_run": dict(latest_run) if latest_run else None,
        "imported_totals": [dict(r) for r in imported],
        "database_totals": totals,
        "current_run": run_summary,
        "important_note": "Daily results are merged into result_runners/races, the same main historical database used by profiles and results pages.",
    }
    _write_json("admin_run_log.json", data)
    return data


def build_all_outputs(run_summary: dict | None = None) -> None:
    build_latest_results_review()
    build_latest_racecards()
    build_admin_status(run_summary)
