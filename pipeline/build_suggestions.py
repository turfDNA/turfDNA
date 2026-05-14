from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, datetime
from statistics import mean
from typing import Any

from .db import connect
from .identity import normalise_horse_name
from .utils import safe_float


def _payload(row: Any) -> dict:
    return json.loads(row["payload_json"])


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _json_dict(text):
    try:
        return json.loads(text or "{}")
    except Exception:
        return {}



def _lookup_stat_bonus(stats_text: object, wanted: object, label: str) -> dict | None:
    """Convert cached condition records into a small XP suitability bonus."""
    wanted_text = str(wanted or "").strip()
    if not wanted_text:
        return None
    stats = _json_dict(stats_text)
    if not isinstance(stats, dict) or not stats:
        return None
    # Exact first, then simple contains match for distances/going descriptions.
    match = None
    for key, val in stats.items():
        k = str(key or "").strip()
        if k.lower() == wanted_text.lower() or k.lower() in wanted_text.lower() or wanted_text.lower() in k.lower():
            match = val
            break
    if not isinstance(match, dict):
        return None
    runs = int(match.get("runs") or 0)
    if runs <= 0:
        return None
    places = int(match.get("places") or 0)
    wins = int(match.get("wins") or 0)
    place_rate = (places / runs) * 100
    win_rate = (wins / runs) * 100
    bonus = _clamp((place_rate - 28) * 0.08 + (win_rate - 8) * 0.08, -4.0, 5.5)
    return {"label": label, "value": round(bonus, 1), "evidence": f"{wins} wins / {places} places / {runs} runs"}

def _form_score(form: object) -> float:
    text = str(form or "").upper()
    if not text or text == "NAN":
        return 0.0
    score = 0.0
    digits = [int(x) for x in re.findall(r"[1-9]", text[-7:])]
    for pos in digits[-4:]:
        score += max(0, 8 - pos) * 0.8
    for bad in ["P", "U", "F", "R", "B"]:
        score -= text.count(bad) * 2.0
    return _clamp(score, -10.0, 14.0)


def _distance_bucket(distance_m=None, distance_text=None) -> str:
    d = safe_float(distance_m)
    if d is None and distance_text:
        txt = str(distance_text).lower()
        nums = re.findall(r"\d+(?:\.\d+)?", txt)
        if "f" in txt and nums:
            f = float(nums[-1])
            if f <= 6: return "sprint"
            if f <= 9: return "mile-ish"
            if f <= 12: return "middle"
            return "staying"
    if not d:
        return "unknown"
    if d <= 1300: return "sprint"
    if d <= 1800: return "mile-ish"
    if d <= 2400: return "middle"
    return "staying"


def _days_since(value):
    if not value:
        return None
    try:
        d = datetime.fromisoformat(str(value)[:10]).date()
        return (date.today() - d).days
    except Exception:
        return None


def _profile_for_payload(payload: dict, profiles: dict[str, dict]) -> dict | None:
    hid = payload.get("runner_horse_id")
    if hid and hid in profiles:
        return profiles[hid]
    key = "name::" + normalise_horse_name(payload.get("runner_horse"))
    return profiles.get(key)


def _load_profiles() -> dict[str, dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM horse_feature_cache").fetchall()
        aliases = conn.execute("SELECT source_horse_id, canonical_horse_id FROM horse_identity_map").fetchall()
    profiles = {r["horse_id"]: dict(r) for r in rows}
    for a in aliases:
        if a["canonical_horse_id"] in profiles:
            profiles[a["source_horse_id"]] = profiles[a["canonical_horse_id"]]
    return profiles


def expected_performance_breakdown(payload: dict, profile: dict | None = None) -> dict:
    """Phase 2 XP engine.

    Non-ML and explainable. It combines historical ability/profile with today's racecard
    clues. The result is not presented as a guaranteed prediction; it is the system's
    data-suggested expected performance score.
    """
    parts = [{"label": "Starting point", "value": 50.0}]

    if profile:
        runs = int(profile.get("runs") or 0)
        if runs:
            parts.append({"label": "Experience", "value": round(_clamp(runs * 0.55, 0, 8), 1)})
            strike = float(profile.get("strike_rate") or 0)
            place = float(profile.get("placing_rate") or 0)
            parts.append({"label": "Win strike rate", "value": round(_clamp((strike - 8) * 0.18, -4, 7), 1)})
            parts.append({"label": "Placing rate", "value": round(_clamp((place - 25) * 0.14, -5, 8), 1)})
        else:
            parts.append({"label": "No historical profile yet", "value": -5.0})

        avg5 = safe_float(profile.get("avg_rpr_last5"))
        latest = safe_float(profile.get("latest_rpr"))
        trend = safe_float(profile.get("rpr_trend"))
        best = safe_float(profile.get("best_rpr"))
        if avg5 is not None:
            parts.append({"label": "Recent RPR level", "value": round(_clamp((avg5 - 75) * 0.16, -8, 12), 1)})
        if latest is not None and best is not None and best > 0:
            parts.append({"label": "Near best rating", "value": round(_clamp((latest / best - 0.86) * 30, -4, 5), 1)})
        if trend is not None:
            parts.append({"label": "Rating momentum", "value": round(_clamp(trend * 0.35, -6, 7), 1)})

        days = _days_since(profile.get("last_run_date"))
        if days is not None:
            if 10 <= days <= 60:
                rec = 4.0
            elif 61 <= days <= 120:
                rec = 1.5
            elif days < 10:
                rec = -1.5
            else:
                rec = -3.0
            parts.append({"label": "Recent run timing", "value": rec})
    else:
        parts.append({"label": "No historical profile yet", "value": -5.0})

    if profile:
        course_bonus = _lookup_stat_bonus(profile.get("course_stats_json"), payload.get("race_course"), "Course suitability")
        going_bonus = _lookup_stat_bonus(profile.get("going_stats_json"), payload.get("race_going"), "Going suitability")
        distance_bonus = _lookup_stat_bonus(profile.get("distance_stats_json"), payload.get("race_distance") or payload.get("race_distance_round"), "Distance suitability")
        for b in [course_bonus, going_bonus, distance_bonus]:
            if b:
                parts.append({"label": b["label"] + " (" + b["evidence"] + ")", "value": b["value"]})

    odds = safe_float(payload.get("runner_odds_decimal") or payload.get("runner_sp_dec"))
    if odds:
        parts.append({"label": "Market guide", "value": round(_clamp(16.0 / max(1.01, odds), 0.5, 12), 1)})

    rpr = safe_float(payload.get("runner_rpr"))
    if rpr is not None:
        parts.append({"label": "Racecard RPR", "value": round(_clamp((rpr - 82) * 0.18, -8, 10), 1)})
    official = safe_float(payload.get("runner_ofr") or payload.get("runner_or"))
    if official is not None:
        parts.append({"label": "Official rating", "value": round(_clamp((official - 72) * 0.10, -6, 8), 1)})

    form = _form_score(payload.get("runner_form"))
    if form:
        parts.append({"label": "Racecard form string", "value": round(form, 1)})

    draw = safe_float(payload.get("runner_draw"))
    field = safe_float(payload.get("race_field_size"))
    if draw is not None and field and field >= 8:
        middle = (field + 1) / 2
        draw_score = -abs(draw - middle) / field * 3.0
        parts.append({"label": "Draw balance", "value": round(draw_score, 1)})

    total = round(_clamp(sum(p["value"] for p in parts), 0.0, 100.0), 1)
    hist_runs = int(profile.get("runs") or 0) if profile else 0
    evidence_points = hist_runs
    evidence_points += sum(1 for p in parts if "suitability" in p["label"].lower()) * 3
    evidence_points += 2 if safe_float(payload.get("runner_rpr")) is not None else 0
    evidence_points += 2 if safe_float(payload.get("runner_odds_decimal") or payload.get("runner_sp_dec")) is not None else 0
    confidence_score = int(_clamp(25 + evidence_points * 5, 20, 95))
    confidence = "High" if confidence_score >= 75 else "Medium" if confidence_score >= 50 else "Low"
    return {"total": total, "confidence": confidence, "confidence_score": confidence_score, "parts": parts, "xp_version": "XP v2 Expected Performance"}


def expected_performance_score(payload: dict, profile: dict | None = None) -> float:
    return expected_performance_breakdown(payload, profile)["total"]


def build_suggestions_for_latest_racecard() -> list[dict]:
    profiles = _load_profiles()
    with connect() as conn:
        latest_row = conn.execute("SELECT race_date FROM racecards_raw WHERE race_date IS NOT NULL ORDER BY race_date DESC LIMIT 1").fetchone()
        if not latest_row:
            return []
        latest = latest_row["race_date"]
        rows = conn.execute(
            """
            SELECT * FROM racecards_raw
            WHERE race_date=? AND is_non_runner=0
            ORDER BY race_course, race_off_dt, race_off_time
            """,
            (latest,),
        ).fetchall()

    races: dict[str, list[dict]] = defaultdict(list)
    race_meta: dict[str, dict] = {}
    for row in rows:
        payload = _payload(row)
        race_id = row["race_race_id"] or f'{row["race_course"]}-{row["race_off_time"]}'
        race_meta[race_id] = {
            "race_id": race_id,
            "date": row["race_date"],
            "course": row["race_course"],
            "time": row["race_off_time"],
            "race_name": payload.get("race_race_name"),
        }
        profile = _profile_for_payload(payload, profiles)
        breakdown = expected_performance_breakdown(payload, profile)
        xp = breakdown["total"]
        races[race_id].append({
            "horse": row["runner_horse"], "horse_id": row["runner_horse_id"], "jockey": row["runner_jockey"],
            "trainer": row["runner_trainer"], "silk_url": row["runner_silk_url"], "number": payload.get("runner_number"),
            "draw": payload.get("runner_draw"), "odds_decimal": payload.get("runner_odds_decimal"), "odds_fractional": payload.get("runner_odds_fractional"), "odds_bookmaker": payload.get("runner_odds_bookmaker"), "odds_updated": payload.get("runner_odds_updated"), "form": payload.get("runner_form"),
            "rpr": payload.get("runner_rpr"), "ofr": payload.get("runner_ofr") or payload.get("runner_or"),
            "xp": xp, "score": xp, "xp_breakdown": breakdown, "confidence": breakdown.get("confidence"),
            "historical_runs": profile.get("runs") if profile else 0,
            "historical_strike_rate": profile.get("strike_rate") if profile else 0,
            "historical_placing_rate": profile.get("placing_rate") if profile else 0,
        })

    suggestions = []
    for race_id, runners in races.items():
        ranked = sorted(runners, key=lambda r: (r["xp"], r.get("historical_runs") or 0), reverse=True)
        for idx, runner in enumerate(ranked, start=1):
            runner["rank"] = idx
        suggestions.append({**race_meta[race_id], "suggested_1_2_3": ranked[:3], "runner_count": len(ranked), "ranked_runners": ranked})

    return sorted(suggestions, key=lambda r: (r.get("course") or "", r.get("time") or ""))
