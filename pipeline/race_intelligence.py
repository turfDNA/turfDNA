from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from statistics import mean
from typing import Any

from .db import connect
from .utils import safe_float


def _clean_text(v: Any) -> str:
    if v is None:
        return ""
    text = str(v).strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def _num(v: Any) -> float | None:
    if v is None:
        return None
    text = str(v).replace("£", "").replace(",", "").strip()
    if not text or text.lower() in {"nan", "none", "null", "-", "–"}:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    if not m:
        return None
    try:
        return float(m.group())
    except Exception:
        return None


def _distance_bucket(distance_text: str | None, distance_m: Any = None) -> str:
    metres = _num(distance_m)
    text = _clean_text(distance_text).lower()
    if metres is None and text:
        miles = 0.0
        furlongs = 0.0
        m = re.search(r"(\d+)m", text)
        f = re.search(r"(\d+)f", text)
        if m:
            miles = float(m.group(1))
        if f:
            furlongs = float(f.group(1))
        if miles or furlongs:
            metres = (miles * 8 + furlongs) * 201.168
    if metres is None:
        return "unknown"
    if metres <= 1250:
        return "sprint_5_6f"
    if metres <= 1650:
        return "short_7f_1m"
    if metres <= 2100:
        return "middle_1m_1m2f"
    if metres <= 2850:
        return "staying_1m4f_plus"
    return "long_staying"


def _draw_bucket(draw: Any, field_size: int | None = None) -> str:
    d = _num(draw)
    if d is None:
        return "unknown"
    if field_size and field_size >= 3:
        third = field_size / 3
        if d <= third:
            return "low"
        if d <= third * 2:
            return "middle"
        return "high"
    if d <= 4:
        return "low"
    if d <= 8:
        return "middle"
    return "high"


def _pace_style(comment: Any) -> str:
    text = _clean_text(comment).lower()
    if not text:
        return "unknown"
    front = ["made all", "led", "soon led", "disputed lead", "prominent", "tracked leader", "pressed leader", "made most"]
    held = ["held up", "towards rear", "in rear", "slowly away", "dwelt", "waited", "detached"]
    strong_finish = ["stayed on", "kept on", "ran on", "finished well", "headway", "late headway"]
    weak = ["weakened", "faded", "tired", "no extra", "lost place"]
    if any(x in text for x in front):
        return "front/prominent"
    if any(x in text for x in held):
        return "held-up"
    if any(x in text for x in strong_finish):
        return "closer"
    if any(x in text for x in weak):
        return "weakened"
    return "midfield/unknown"


def _percent(part: int, total: int) -> int:
    return int(round((part / total) * 100)) if total else 0


def _top_counts(counter: Counter, limit: int = 3):
    total = sum(counter.values())
    return [
        {"label": str(k), "count": int(v), "pct": _percent(v, total)}
        for k, v in counter.most_common(limit)
        if k not in {"", "unknown", None}
    ]


def _avg(values):
    vals = [float(v) for v in values if v is not None and not math.isnan(float(v))]
    return round(mean(vals), 1) if vals else None


def _detect_race_pace(runners: list[dict]) -> dict:
    front_clues = 0
    hold_up_clues = 0
    unknown = 0
    for r in runners:
        text = " ".join(str(r.get(k) or "") for k in ["form", "comment", "runner_comment", "racecard_comment"]).lower()
        # Racecards usually do not contain comment style, so form-based clues are intentionally simple.
        if any(w in text for w in ["led", "prominent", "made all", "front"]):
            front_clues += 1
        elif any(w in text for w in ["held up", "rear", "slowly"]):
            hold_up_clues += 1
        else:
            unknown += 1
    if front_clues >= 3:
        label = "Likely strong early pace"
        note = "Several runners show front-running or prominent clues. Leaders may face pressure."
    elif front_clues == 1:
        label = "Possible easy lead"
        note = "Only one clear front-running clue found, so a leader may get control."
    elif front_clues == 0:
        label = "Pace unclear"
        note = "Not enough run-style clues in today’s card yet."
    else:
        label = "Balanced pace"
        note = "A normal early pace looks most likely from available clues."
    return {"label": label, "note": note, "front_runner_clues": front_clues, "hold_up_clues": hold_up_clues, "unknown": unknown}


def build_race_intelligence(course: str | None, distance: str | None = None, going: str | None = None, surface: str | None = None, runners: list[dict] | None = None) -> dict:
    """Infer race-level track/racecard intelligence from historical results.

    This intentionally uses simple, explainable calculations. It should be considered
    a guide, not a guaranteed prediction.
    """
    course_name = _clean_text(course) or "Unknown course"
    runners = runners or []
    race_distance_bucket = _distance_bucket(distance)
    surface_text = _clean_text(surface).lower()
    going_text = _clean_text(going).lower()

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT r.race_id, r.race_date, r.course, r.distance_text, r.distance_m, r.going, r.surface,
                   rr.finish_position, rr.draw, rr.rpr, rr.official_rating, rr.sp_dec,
                   rr.trainer, rr.jockey, rr.comment
            FROM result_runners rr
            JOIN races r ON r.race_id = rr.race_id
            WHERE LOWER(COALESCE(r.course,'')) = LOWER(?)
              AND rr.finish_position IS NOT NULL
            """,
            (course_name,),
        ).fetchall()

    all_rows = [dict(r) for r in rows]
    for r in all_rows:
        r["distance_bucket"] = _distance_bucket(r.get("distance_text"), r.get("distance_m"))
        r["pace_style"] = _pace_style(r.get("comment"))

    matched = all_rows
    distance_matched = [r for r in matched if race_distance_bucket != "unknown" and r["distance_bucket"] == race_distance_bucket]
    if len(distance_matched) >= 20:
        matched = distance_matched
    if surface_text:
        surf_matched = [r for r in matched if surface_text in _clean_text(r.get("surface")).lower()]
        if len(surf_matched) >= 15:
            matched = surf_matched
    if going_text:
        going_key = going_text.split()[0]
        going_matched = [r for r in matched if going_key and going_key in _clean_text(r.get("going")).lower()]
        if len(going_matched) >= 12:
            matched = going_matched

    race_ids = {r["race_id"] for r in matched if r.get("race_id")}
    winners = [r for r in matched if int(r.get("finish_position") or 0) == 1]
    placed = [r for r in matched if int(r.get("finish_position") or 0) <= 3]

    field_sizes = Counter()
    for r in matched:
        rid = r.get("race_id")
        if rid:
            field_sizes[rid] += 1

    draw_counter = Counter()
    pace_counter = Counter()
    trainer_counter = Counter()
    jockey_counter = Counter()
    rpr_values = []
    or_values = []
    sp_values = []
    for w in winners:
        fs = field_sizes.get(w.get("race_id"))
        draw_counter[_draw_bucket(w.get("draw"), fs)] += 1
        pace_counter[w.get("pace_style") or "unknown"] += 1
        trainer_counter[_clean_text(w.get("trainer")) or "unknown"] += 1
        jockey_counter[_clean_text(w.get("jockey")) or "unknown"] += 1
        rv = _num(w.get("rpr"))
        ov = _num(w.get("official_rating"))
        sv = _num(w.get("sp_dec"))
        if rv is not None: rpr_values.append(rv)
        if ov is not None: or_values.append(ov)
        if sv is not None: sp_values.append(sv)

    sample_races = len(race_ids)
    sample_runners = len(matched)
    winning_draw = draw_counter.most_common(1)[0][0] if draw_counter else "unknown"
    winning_pace = pace_counter.most_common(1)[0][0] if pace_counter else "unknown"

    if sample_races < 5:
        confidence = "Low"
        data_note = "Limited historical evidence for this course/setup. Treat the panel as a light guide."
    elif sample_races < 25:
        confidence = "Medium"
        data_note = "Useful course evidence, but some angles may still be thin."
    else:
        confidence = "Good"
        data_note = "Good historical sample for course-level guidance."

    draw_label = "No clear draw edge"
    if winning_draw in {"low", "middle", "high"}:
        pct = _percent(draw_counter[winning_draw], max(1, sum(draw_counter.values())))
        draw_label = f"{winning_draw.title()} draws have won most often ({pct}% of winners in sample)"

    pace_label = "No clear run-style edge"
    if winning_pace not in {"", "unknown", "midfield/unknown"}:
        pct = _percent(pace_counter[winning_pace], max(1, sum(pace_counter.values())))
        pace_label = f"{winning_pace.title()} types have won most often ({pct}% of winners in sample)"

    pace_today = _detect_race_pace(runners)

    return {
        "course": course_name,
        "sample": {"races": sample_races, "runners": sample_runners, "winners": len(winners), "placed": len(placed), "confidence": confidence, "note": data_note},
        "course_bias": {
            "headline": pace_label if winning_pace != "unknown" else "Course bias still building",
            "winning_run_styles": _top_counts(pace_counter),
        },
        "draw_bias": {
            "headline": draw_label,
            "winning_draws": _top_counts(draw_counter),
        },
        "pace_setup": pace_today,
        "winning_profile": {
            "avg_winning_rpr": _avg(rpr_values),
            "avg_winning_or": _avg(or_values),
            "avg_winning_sp": _avg(sp_values),
            "distance_bucket": race_distance_bucket,
            "notes": [
                f"Average winning RPR: {_avg(rpr_values) if _avg(rpr_values) is not None else 'not enough data'}",
                f"Average winning OR: {_avg(or_values) if _avg(or_values) is not None else 'not enough data'}",
            ],
        },
        "top_course_connections": {
            "trainers": _top_counts(trainer_counter),
            "jockeys": _top_counts(jockey_counter),
        },
    }
