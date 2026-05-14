from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

from .db import connect, init_db
from .identity import normalise_horse_name
from .utils import safe_float
from .build_daily_outputs import result_run_score, result_score_breakdown
from .build_suggestions import expected_performance_breakdown


def _payload(row: Any) -> dict:
    try:
        return json.loads(row["payload_json"] or "{}")
    except Exception:
        return {}


def _pos_number(value) -> int | None:
    if value is None:
        return None
    m = re.search(r"\d+", str(value))
    return int(m.group()) if m else None


def _money(value: object) -> float:
    if value is None:
        return 0.0
    text = str(value).replace("£", "").replace(",", "").strip()
    return safe_float(text) or 0.0


def _json(text, default):
    try:
        return json.loads(text or "")
    except Exception:
        return default




def _avg(vals):
    vals = [float(v) for v in vals if v is not None]
    return round(sum(vals)/len(vals), 2) if vals else None

def _rate_pct(num, den):
    return round((num/den)*100, 1) if den else 0.0

def _record_from_rows(rows, key_func):
    groups = defaultdict(list)
    for r in rows:
        key = key_func(r) or "Unknown"
        groups[str(key)].append(r)
    out=[]
    for key, rs in groups.items():
        runs=len(rs)
        wins=sum(1 for r in rs if r["finish_position"] == 1)
        places=sum(1 for r in rs if r["finish_position"] and r["finish_position"] <= 3)
        rprs=[r["rpr"] for r in rs if r["rpr"] is not None]
        out.append({"label":key,"runs":runs,"wins":wins,"places":places,"strike_rate":_rate_pct(wins,runs),"placing_rate":_rate_pct(places,runs),"avg_rpr":_avg(rprs)})
    return sorted(out, key=lambda x:(-x["placing_rate"],-x["wins"],-x["runs"],x["label"]))[:10]

def _days_between(a, b):
    from datetime import datetime
    try:
        da=datetime.fromisoformat(str(a)[:10])
        db=datetime.fromisoformat(str(b)[:10])
        return abs((db-da).days)
    except Exception:
        return None


def _comment_intelligence(rows):
    text_rows=[]
    hidden=[]
    positive=[]
    negative=[]
    for r in rows or []:
        c=str(r["comment"] or "").strip()
        if not c:
            continue
        lc=c.lower()
        horse_run={"date":r["race_date"],"course":r["course"],"position":r["position_raw"],"comment":c}
        if any(w in lc for w in ["hampered","short of room","not clear run","denied clear run","blocked","checked","interference","slowly away","dwelt","wide"]):
            hidden.append(horse_run)
        if any(w in lc for w in ["stayed on","ran on","finished well","late headway","kept on well","headway"]):
            positive.append(horse_run)
        if any(w in lc for w in ["weakened","faded","no extra","eased","tailed off","struggling"]):
            negative.append(horse_run)
        text_rows.append(lc)
    return {
        "hidden_form_runs": hidden[-8:],
        "positive_finish_runs": positive[-8:],
        "negative_finish_runs": negative[-8:],
        "hidden_form_score": min(100, len(hidden)*14 + len(positive)*6),
        "note": "Uses race comments to spot runs that may be better or worse than the bare finishing position."
    }


def _class_ceiling(rows):
    by_class=defaultdict(list)
    for r in rows or []:
        key=str(r["race_class"] or "Unknown")
        by_class[key].append(r)
    out=[]
    for k, rs in by_class.items():
        runs=len(rs)
        wins=sum(1 for r in rs if r["finish_position"] == 1)
        places=sum(1 for r in rs if r["finish_position"] and r["finish_position"] <= 3)
        rprs=[r["rpr"] for r in rs if r["rpr"] is not None]
        out.append({"class":k,"runs":runs,"wins":wins,"places":places,"place_rate":_rate_pct(places,runs),"avg_rpr":_avg(rprs),"best_rpr":max(rprs) if rprs else None})
    out=sorted(out, key=lambda x: (-(x.get("best_rpr") or 0), -x["place_rate"], -x["runs"]))
    if not out:
        return {"label":"Unknown","records":[]}
    best=out[0]
    return {"label":f"Best evidence: {best['class']}","records":out[:8]}


def _race_strength(rows):
    # Simple race-strength proxy: stronger races tend to have higher winning/field RPR.
    vals=[r["rpr"] for r in rows or [] if r["rpr"] is not None]
    if not vals:
        return {"score":None,"label":"Not enough RPR data","note":"Race strength needs more completed runs with RPR ratings."}
    avg=_avg(vals)
    best=max(vals)
    score=max(0,min(100,round((avg-55)*1.15 + (best-avg)*0.25)))
    label="Strong historical races" if score>=75 else "Average historical strength" if score>=45 else "Lower-grade evidence"
    return {"score":score,"label":label,"avg_rpr":avg,"best_rpr":best,"note":"Proxy based on the horse's achieved RPR levels across its completed races."}


def _market_outcomes(rows):
    market=[]
    for r in rows or []:
        sp=r["sp_dec"]
        pos=r["finish_position"]
        if sp is None or pos is None:
            continue
        expected_rank = "short" if float(sp)<=4 else "middle" if float(sp)<=10 else "big"
        outperformed = (expected_rank=="big" and pos<=4) or (expected_rank=="middle" and pos<=3) or (expected_rank=="short" and pos==1)
        disappointed = (expected_rank=="short" and pos>3) or (expected_rank=="middle" and pos>6)
        market.append({"date":r["race_date"],"course":r["course"],"sp":sp,"position":pos,"outperformed":outperformed,"disappointed":disappointed})
    return {
        "outperformed_count":sum(1 for x in market if x["outperformed"]),
        "disappointed_count":sum(1 for x in market if x["disappointed"]),
        "sample":market[-12:],
        "note":"Compares finishing position with broad SP expectation. It is not true steam/drift data because we do not yet have pre-race odds movement."
    }


def _suitability_snapshot(rows):
    def top(label, key):
        rec=_record_from_rows(rows, key)
        if not rec:
            return {"type":label,"value":"Unknown","confidence":"Low"}
        item=rec[0]
        conf="High" if item["runs"]>=5 else "Medium" if item["runs"]>=2 else "Low"
        return {"type":label,"value":item["label"],"runs":item["runs"],"wins":item["wins"],"places":item["places"],"placing_rate":item["placing_rate"],"confidence":conf}
    return [
        top("Course", lambda r:r["course"]),
        top("Going", lambda r:r["going"]),
        top("Distance", lambda r:r["distance_text"]),
        top("Surface", lambda r:r["surface"]),
        top("Class", lambda r:r["race_class"]),
    ]

def _infer_intelligence(full_rows):
    # full_rows are newest first; work oldest -> newest for trends.
    rows=list(reversed(full_rows or []))
    if not rows:
        return {
            "development":{"label":"Not enough data","note":"Add more historical runs to build this profile."},
            "confidence":{"score":0,"label":"Low","reasons":["No historical runs loaded yet"]},
            "visual_series":{}
        }
    rprs=[r["rpr"] for r in rows if r["rpr"] is not None]
    ors=[r["official_rating"] for r in rows if r["official_rating"] is not None]
    positions=[r["finish_position"] for r in rows if r["finish_position"] is not None]
    trend=None
    if len(rprs)>=6:
        trend=round(_avg(rprs[-3:]) - _avg(rprs[-6:-3]), 1)
    elif len(rprs)>=2:
        trend=round(float(rprs[-1]) - float(rprs[0]), 1)
    if trend is None:
        dev_label="Not enough rating data"
    elif trend>=6:
        dev_label="Strong improver"
    elif trend>=2:
        dev_label="Improving"
    elif trend<=-6:
        dev_label="Regressing"
    elif trend<=-2:
        dev_label="Slightly down"
    else:
        dev_label="Stable"

    # consistency: lower spread in positions/RPR means higher score.
    import statistics
    pos_sd=statistics.pstdev([float(x) for x in positions]) if len(positions)>1 else 0
    rpr_sd=statistics.pstdev([float(x) for x in rprs]) if len(rprs)>1 else 0
    consistency=max(0, min(100, round(100 - pos_sd*8 - rpr_sd*1.2)))

    comments=" ".join(str(r["comment"] or "").lower() for r in rows)
    pace_counts={
        "Front runner": sum(comments.count(w) for w in ["made all","led","quickly away","soon led"]),
        "Prominent": sum(comments.count(w) for w in ["prominent","tracked leader","chased leader","close up"]),
        "Midfield": sum(comments.count(w) for w in ["midfield","in touch"]),
        "Hold-up": sum(comments.count(w) for w in ["held up","towards rear","in rear","waited with"]),
        "Finishes strongly": sum(comments.count(w) for w in ["stayed on","ran on","kept on","finished well"]),
        "Weakens late": sum(comments.count(w) for w in ["weakened","faded","no extra"]),
    }
    pace_style=max(pace_counts, key=pace_counts.get) if max(pace_counts.values())>0 else "Unknown"

    # Layoff/freshness buckets based on days since previous run.
    freshness={"quick_return":{"runs":0,"avg_rpr":None},"normal":{"runs":0,"avg_rpr":None},"fresh":{"runs":0,"avg_rpr":None}}
    buckets={"quick_return":[],"normal":[],"fresh":[]}
    for prev, cur in zip(rows, rows[1:]):
        gap=_days_between(prev["race_date"], cur["race_date"])
        if gap is None: continue
        key="quick_return" if gap<=14 else "fresh" if gap>=60 else "normal"
        buckets[key].append(cur)
    for k, rs in buckets.items():
        freshness[k]={"runs":len(rs),"avg_rpr":_avg([r["rpr"] for r in rs if r["rpr"] is not None]),"placing_rate":_rate_pct(sum(1 for r in rs if r["finish_position"] and r["finish_position"]<=3), len(rs))}

    market_rows=[r for r in rows if r["sp_dec"] is not None]
    short_price=[r for r in market_rows if float(r["sp_dec"])<=5]
    market={
        "avg_sp": _avg([r["sp_dec"] for r in market_rows]),
        "short_price_runs": len(short_price),
        "short_price_place_rate": _rate_pct(sum(1 for r in short_price if r["finish_position"] and r["finish_position"]<=3), len(short_price)),
        "note": "Uses available SP/BSP history; true price movement needs pre-race odds snapshots."
    }

    # Draw buckets.
    def draw_bucket(r):
        try:
            d=int(str(r["draw"]).strip())
        except Exception:
            return "Unknown"
        if d<=4: return "Low"
        if d<=8: return "Middle"
        return "High"

    class_record=_record_from_rows(rows, lambda r: r["race_class"])
    surface_record=_record_from_rows(rows, lambda r: r["surface"])
    draw_record=_record_from_rows(rows, draw_bucket)
    trainer_record=_record_from_rows(rows, lambda r: r["trainer"])
    jockey_record=_record_from_rows(rows, lambda r: r["jockey"])
    trainer_jockey_record=_record_from_rows(rows, lambda r: f"{r['trainer'] or 'Unknown'} + {r['jockey'] or 'Unknown'}")

    best_conditions=[]
    for title, rec in [("Course", _record_from_rows(rows, lambda r:r["course"])), ("Going", _record_from_rows(rows, lambda r:r["going"])), ("Distance", _record_from_rows(rows, lambda r:r["distance_text"])), ("Surface", surface_record)]:
        if rec:
            top=rec[0]
            best_conditions.append({"type":title,"value":top["label"],"runs":top["runs"],"wins":top["wins"],"places":top["places"],"placing_rate":top["placing_rate"],"avg_rpr":top["avg_rpr"]})

    # Confidence for today/future suggestions.
    runs=len(rows)
    conf=25 + min(35, runs*4) + min(20, consistency/5)
    if rprs: conf += 10
    if len(rows)>=5: conf += 10
    conf=max(0, min(100, round(conf)))
    conf_label="High" if conf>=75 else "Medium" if conf>=50 else "Low"
    reasons=[]
    reasons.append(f"{runs} historical runs loaded")
    reasons.append(f"Consistency score {consistency}/100")
    if trend is not None: reasons.append(f"RPR trend {trend:+}")

    visual_series={
        "finishing_positions":[{"date":r["race_date"],"course":r["course"],"value":r["finish_position"]} for r in rows if r["finish_position"] is not None][-20:],
        "rpr":[{"date":r["race_date"],"course":r["course"],"value":r["rpr"]} for r in rows if r["rpr"] is not None][-20:],
        "official_rating":[{"date":r["race_date"],"course":r["course"],"value":r["official_rating"]} for r in rows if r["official_rating"] is not None][-20:],
        "prize_cumulative":[],
    }
    total=0.0
    for r in rows:
        total += float(r["prize"] or 0)
        visual_series["prize_cumulative"].append({"date":r["race_date"],"course":r["course"],"value":round(total,2)})
    visual_series["prize_cumulative"] = visual_series["prize_cumulative"][-20:]

    return {
        "development":{"label":dev_label,"rpr_trend":trend,"note":"Compares recent RPR ratings against older RPR ratings."},
        "best_conditions":best_conditions,
        "pace":{"style":pace_style,"signals":pace_counts},
        "market":market,
        "market_outcomes": _market_outcomes(rows),
        "hidden_form": _comment_intelligence(rows),
        "class_ceiling": _class_ceiling(rows),
        "race_strength": _race_strength(rows),
        "suitability_snapshot": _suitability_snapshot(rows),
        "consistency":{"score":consistency,"label":"Reliable" if consistency>=75 else "Mixed" if consistency>=45 else "Volatile"},
        "class_record":class_record,
        "surface_record":surface_record,
        "freshness":freshness,
        "draw_record":draw_record,
        "trainer_record":trainer_record,
        "jockey_record":jockey_record,
        "trainer_jockey_record":trainer_jockey_record,
        "confidence":{"score":conf,"label":conf_label,"reasons":reasons},
        "visual_series":visual_series,
    }


def _canonical_for(conn, horse_id: str | None, horse: str | None) -> tuple[str | None, str | None]:
    if horse_id:
        row = conn.execute("SELECT canonical_horse_id FROM horse_identity_map WHERE source_horse_id=?", (horse_id,)).fetchone()
        if row:
            return row["canonical_horse_id"], horse_id
        row = conn.execute("SELECT canonical_horse_id FROM horse_master WHERE canonical_horse_id=?", (horse_id,)).fetchone()
        if row:
            return row["canonical_horse_id"], horse_id
    norm = normalise_horse_name(horse)
    if norm:
        row = conn.execute("SELECT canonical_horse_id FROM horse_master WHERE normalised_name=? ORDER BY last_seen_date DESC LIMIT 1", (norm,)).fetchone()
        if row:
            return row["canonical_horse_id"], None
        return f"name::{norm}", None
    return None, horse_id


def _name_variants(horse: str | None) -> list[str]:
    if not horse:
        return []
    raw = str(horse).strip()
    variants = {raw, raw.lower()}
    # common apostrophe/quote differences from CSV/browser values
    variants.add(raw.replace("’", "'"))
    variants.add(raw.replace("'", "’"))
    variants.add(raw.replace("`", "'"))
    return [v for v in variants if v]


def _source_ids(conn, canonical_id: str | None, original_id: str | None = None) -> list[str]:
    ids = set()
    if original_id:
        ids.add(original_id)
    if canonical_id:
        ids.add(canonical_id)
        for row in conn.execute("SELECT source_horse_id FROM horse_identity_map WHERE canonical_horse_id=?", (canonical_id,)).fetchall():
            if row["source_horse_id"]:
                ids.add(row["source_horse_id"])
    return list(ids)


def _fallback_latest_rows(horse_id: str | None, horse: str | None) -> dict:
    # Kept deliberately small: it means the page still opens before the user has run the full import.
    from .config import OUTPUTS_DIR
    wanted_norm = normalise_horse_name(horse)
    results = []
    cards = []
    for filename, target in [("latest_results_review.json", results), ("latest_racecards.json", cards)]:
        path = OUTPUTS_DIR / filename
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for course in data.get("courses", []):
            for race in course.get("races", []):
                rows = race.get("runners", [])
                if filename == "latest_racecards.json":
                    rows += race.get("suggested_1_2_3", [])
                for r in rows:
                    if (horse_id and str(r.get("horse_id")) == str(horse_id)) or (wanted_norm and normalise_horse_name(r.get("horse")) == wanted_norm):
                        rr = dict(r)
                        rr.update({"date": data.get("date"), "course": course.get("course"), "time": race.get("time"), "race_name": race.get("race_name")})
                        target.append(rr)
    return {"results": results, "cards": cards}


def build_horse_profile(horse_id: str | None = None, horse: str | None = None) -> dict:
    if not horse_id and not horse:
        return {"found": False, "message": "No horse selected."}

    init_db()
    with connect() as conn:
        canonical_id, original_id = _canonical_for(conn, horse_id, horse)
        source_ids = _source_ids(conn, canonical_id, original_id)
        norm = normalise_horse_name(horse)

        cache = conn.execute("SELECT * FROM horse_feature_cache WHERE horse_id=?", (canonical_id,)).fetchone() if canonical_id else None
        master = conn.execute("SELECT * FROM horse_master WHERE canonical_horse_id=?", (canonical_id,)).fetchone() if canonical_id else None

        result_rows = []
        card_rows = []
        if source_ids:
            placeholders = ",".join("?" for _ in source_ids)
            result_rows = conn.execute(
                f"SELECT * FROM results_raw WHERE runner_horse_id IN ({placeholders}) ORDER BY race_date DESC, race_off_dt DESC",
                source_ids,
            ).fetchall()
            card_rows = conn.execute(
                f"SELECT * FROM racecards_raw WHERE runner_horse_id IN ({placeholders}) ORDER BY race_date DESC, race_off_dt DESC",
                source_ids,
            ).fetchall()
        if not result_rows and norm:
            for variant in _name_variants(horse):
                result_rows = conn.execute(
                    "SELECT * FROM results_raw WHERE lower(trim(runner_horse))=? ORDER BY race_date DESC, race_off_dt DESC", (variant.lower(),)
                ).fetchall()
                if result_rows:
                    break
        if not card_rows and norm:
            for variant in _name_variants(horse):
                card_rows = conn.execute(
                    "SELECT * FROM racecards_raw WHERE lower(trim(runner_horse))=? ORDER BY race_date DESC, race_off_dt DESC", (variant.lower(),)
                ).fetchall()
                if card_rows:
                    break

        # If there is no cached profile but the raw rows exist, still build a full profile from normalized result_runners.
        # This prevents the drawer from falling back to latest-page-only data after a partial import.
        if not cache and result_rows and not canonical_id:
            canonical_id = result_rows[0]["runner_horse_id"] or f"name::{norm}"
            source_ids = _source_ids(conn, canonical_id, result_rows[0]["runner_horse_id"])

        if not cache and not result_rows and not card_rows:
            fallback = _fallback_latest_rows(horse_id, horse)
            if not fallback["results"] and not fallback["cards"]:
                return {"found": False, "message": "No horse data found yet. Try running the daily update after uploading results/racecards."}
            name = horse or (fallback["results"] or fallback["cards"])[0].get("horse")
            return {
                "found": True, "horse_id": horse_id, "horse": name, "silk_url": (fallback["results"] or fallback["cards"])[0].get("silk_url"),
                "data_warning": "Showing latest page data only. Run the historical import to unlock the full profile.",
                "summary": {"runs": len(fallback["results"]), "wins": 0, "places": 0, "strike_rate": 0, "placing_rate": 0, "prize_money": 0, "best_rpr": None, "latest_rpr": None},
                "future_entries": fallback["cards"], "recent_runs": fallback["results"], "rating_movement": [], "course_record": [], "going_record": [], "distance_record": [], "xp_history": [],
            }

        # Full result rows from normalized result_runners/races are more reliable than raw JSON.
        full_rows = []
        if source_ids:
            placeholders = ",".join("?" for _ in source_ids)
            full_rows = conn.execute(
                f"""
                SELECT rr.*, r.race_date, r.course, r.off_time, r.off_dt, r.race_name, r.going, r.surface, r.distance_m, r.distance_text,
                       r.race_class, r.race_type
                FROM result_runners rr JOIN races r ON r.race_id = rr.race_id
                WHERE rr.horse_id IN ({placeholders})
                ORDER BY r.race_date DESC, r.off_dt DESC
                """,
                source_ids,
            ).fetchall()
        if not full_rows and norm:
            for variant in _name_variants(horse):
                full_rows = conn.execute(
                    """
                    SELECT rr.*, r.race_date, r.course, r.off_time, r.off_dt, r.race_name, r.going, r.surface, r.distance_m, r.distance_text,
                           r.race_class, r.race_type
                    FROM result_runners rr JOIN races r ON r.race_id = rr.race_id
                    WHERE lower(trim(rr.horse_name))=?
                    ORDER BY r.race_date DESC, r.off_dt DESC
                    """,
                    (variant.lower(),),
                ).fetchall()
                if full_rows:
                    break

    cache_d = dict(cache) if cache else {}
    master_d = dict(master) if master else {}
    name = cache_d.get("horse_name") or master_d.get("horse_name") or horse or (result_rows[0]["runner_horse"] if result_rows else card_rows[0]["runner_horse"])
    silk = master_d.get("latest_silk_url") or next((r["runner_silk_url"] for r in list(card_rows) + list(result_rows) if r["runner_silk_url"]), None)

    if cache_d:
        runs = int(cache_d.get("runs") or 0)
        wins = int(cache_d.get("wins") or 0)
        places = int(cache_d.get("places") or 0)
        prize = float(cache_d.get("prize_money") or 0)
    else:
        runs = len(full_rows) or len(result_rows)
        wins = sum(1 for r in full_rows if r["finish_position"] == 1)
        places = sum(1 for r in full_rows if r["finish_position"] and r["finish_position"] <= 3)
        prize = sum(float(r["prize"] or 0) for r in full_rows) if full_rows else 0.0

    course_stats = _json(cache_d.get("course_stats_json"), {}) if cache_d else {}
    going_stats = _json(cache_d.get("going_stats_json"), {}) if cache_d else {}
    distance_stats = _json(cache_d.get("distance_stats_json"), {}) if cache_d else {}
    cached_recent = _json(cache_d.get("recent_runs_json"), []) if cache_d else []

    recent_runs = []
    rating_points = []
    xp_history = []
    if full_rows:
        for row in full_rows[:20]:
            p = _payload(row)
            xp_break = result_score_breakdown(p)
            recent_runs.append({
                "date": row["race_date"], "course": row["course"], "time": row["off_time"], "race_name": row["race_name"],
                "position": row["position_raw"], "sp": row["sp_dec"], "rpr": row["rpr"], "or": row["official_rating"],
                "tsr": row["tsr"], "beaten_distance": row["ovr_btn"] if row["ovr_btn"] is not None else row["btn"],
                "comment": row["comment"], "prize": row["prize"], "xp": result_run_score(p), "xp_breakdown": xp_break,
            })
            xp_history.append({"date": row["race_date"], "course": row["course"], "race_name": row["race_name"], "xp": result_run_score(p), "type": "result"})
        for row in reversed(full_rows):
            if row["rpr"] is not None or row["official_rating"] is not None:
                rating_points.append({"date": row["race_date"], "course": row["course"], "race_name": row["race_name"], "rpr": row["rpr"], "official": row["official_rating"], "tsr": row["tsr"]})
    else:
        recent_runs = cached_recent

    future_entries = []
    profile_for_xp = cache_d if cache_d else None
    for row in list(card_rows)[:10]:
        p = _payload(row)
        breakdown = expected_performance_breakdown(p, profile_for_xp)
        future_entries.append({
            "date": row["race_date"], "course": row["race_course"], "time": row["race_off_time"], "race_name": p.get("race_race_name"),
            "jockey": row["runner_jockey"], "trainer": row["runner_trainer"], "draw": p.get("runner_draw"), "number": p.get("runner_number"),
            "odds_decimal": p.get("runner_odds_decimal"), "rpr": p.get("runner_rpr"), "ofr": p.get("runner_ofr") or p.get("runner_or"),
            "form": p.get("runner_form"), "xp": breakdown["total"], "xp_breakdown": breakdown,
        })
        xp_history.append({"date": row["race_date"], "course": row["race_course"], "race_name": p.get("race_race_name"), "xp": breakdown["total"], "type": "future"})

    rpr_values = [float(r["rpr"]) for r in full_rows if r["rpr"] is not None]
    def _first_payload_value(keys):
        for row in list(full_rows or []) + list(result_rows or []) + list(card_rows or []):
            p = _payload(row)
            for key in keys:
                v = p.get(key)
                if v not in (None, "", "None", "nan"):
                    return v
        return None

    owner = _first_payload_value(["runner_owner", "owner", "horse_owner"])

    summary = {
        "runs": runs, "wins": wins, "places": places,
        "strike_rate": float(cache_d.get("strike_rate") or (round(wins / runs * 100, 1) if runs else 0)),
        "placing_rate": float(cache_d.get("placing_rate") or (round(places / runs * 100, 1) if runs else 0)),
        "prize_money": round(prize, 2),
        "best_rpr": cache_d.get("best_rpr") or (max(rpr_values) if rpr_values else None), "latest_rpr": cache_d.get("latest_rpr") or (rpr_values[0] if rpr_values else None),
        "avg_rpr_last3": cache_d.get("avg_rpr_last3"), "avg_rpr_last5": cache_d.get("avg_rpr_last5"),
        "rpr_trend": cache_d.get("rpr_trend"), "last_run_date": cache_d.get("last_run_date"),
        "age": master_d.get("latest_age"), "sex": master_d.get("sex"), "trainer": cache_d.get("latest_trainer"),
        "owner": owner, "sire": master_d.get("sire"), "dam": master_d.get("dam"), "damsire": master_d.get("damsire"),
    }

    def top_stats(stats: dict, label: str):
        return sorted([{label: k, **v} for k, v in stats.items()], key=lambda x: (-x.get("wins",0), -x.get("places",0), -x.get("runs",0), str(x.get(label))))[:8]

    intelligence = _infer_intelligence(full_rows)

    return {
        "found": True,
        "horse_id": canonical_id or horse_id,
        "horse": name,
        "silk_url": silk,
        "summary": summary,
        "future_entries": future_entries,
        "recent_runs": recent_runs[:20],
        "rating_movement": rating_points[-20:],
        "course_record": top_stats(course_stats, "course"),
        "going_record": top_stats(going_stats, "going"),
        "distance_record": top_stats(distance_stats, "distance"),
        "xp_history": sorted(xp_history, key=lambda x: x.get("date") or "")[-20:],
        "intelligence": intelligence,
    }
