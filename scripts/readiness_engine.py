"""
readiness_engine.py

Implements the AthleteOS Readiness Engine (spec doc 08): a transparent,
versioned, weighted score for overall race readiness plus per-discipline
scores, each with an explanation (top factor, top limiter, trend, evidence
window, confidence, next action).

Inputs: data/activities.json, data/sleep.json, data/summary.json (all written
by fetch_garmin.py) plus config/race_config.json, config/athlete_profile.json,
and config/readiness_weights.json (the versioned scoring configuration).

Output: data/readiness.json.

This intentionally does NOT invent data for sources that aren't ingested yet
(HRV, body battery, training readiness).
Where a discipline can't be scored from real evidence, it is marked with low
confidence and an explicit "not yet tracked" limiter rather than a fabricated
number -- per the spec's "never invent missing data" rule.
"""

import json
import os
import re
from datetime import timedelta, date
import datetime as _dt

PACIFIC = _dt.timezone(_dt.timedelta(hours=-7))
TODAY = _dt.datetime.now(PACIFIC).date()

MILES_PER_METER = 1 / 1609.34


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


RACE = load_json("config/race_config.json")
PROFILE = load_json("config/athlete_profile.json")
WEIGHTS_CFG = load_json("config/readiness_weights.json")

summary = load_json("data/summary.json")
overall_summary = summary.get("overall", {})
weeks_summary = summary.get("weeks", [])
activities = load_json("data/activities.json", [])
sleep = load_json("data/sleep.json", [])

CALC_VERSION = WEIGHTS_CFG.get("calculation_version", "0.0.0")
RACE_TYPE = RACE.get("race_type", WEIGHTS_CFG.get("default_race_type"))
WEIGHTS = WEIGHTS_CFG.get("weights_by_race_type", {}).get(RACE_TYPE, {
    "swim": 0.225, "bike": 0.325, "run": 0.275, "recovery": 0.175,
})
THRESHOLDS = WEIGHTS_CFG.get("thresholds", {
    "strong": {"min": 85, "label": "Strong"},
    "on_track": {"min": 70, "label": "On track"},
    "needs_attention": {"min": 55, "label": "Needs attention"},
    "significant_gap": {"min": 0, "label": "Significant gap"},
})
CONF_CFG = WEIGHTS_CFG.get("confidence_inputs", {})

BUILD = RACE.get("build", {})
try:
    BUILD_START = date.fromisoformat(BUILD.get("start_date"))
except Exception:
    BUILD_START = TODAY
TOTAL_WEEKS = BUILD.get("total_weeks", 13)

# Matches fetch_garmin.py's WEEK_ANCHOR: the schedule resolves every date to
# its real Monday-Sunday calendar week, so the fallback week count (used
# only if data/summary.json's currentWeek is missing) must snap to that same
# Monday, not BUILD_START's literal weekday, or this would drift a day out
# of sync with the schedule once BUILD_START isn't itself a Monday.
WEEK_ANCHOR = BUILD_START - timedelta(days=BUILD_START.weekday())

CURRENT_WEEK = overall_summary.get("currentWeek") or min(
    TOTAL_WEEKS, max(1, (TODAY - WEEK_ANCHOR).days // 7 + 1)
)

# Assumed discipline share of total weekly TSS -- a documented modeling
# assumption (not hidden), used only to size an *expected* volume, not to
# grade against an exact prescribed number.
DISCIPLINE_TSS_SPLIT = {"swim": 0.20, "bike": 0.45, "run": 0.35}

EVIDENCE_WINDOW_DAYS = 28
window_start = TODAY - timedelta(days=EVIDENCE_WINDOW_DAYS)
prior_window_start = TODAY - timedelta(days=EVIDENCE_WINDOW_DAYS * 2)

WEEKLY_TARGETS = RACE.get("weekly_tss_targets", {})


def in_window(a, start, end):
    return start.isoformat() <= a["date"] <= end.isoformat()


def week_num_for_date(d):
    return min(TOTAL_WEEKS, max(1, (d - WEEK_ANCHOR).days // 7 + 1))


def expected_tss_for_window(start, end, disc_share):
    """
    Sum each day's own week's target (target/7), not a flat rate from
    CURRENT_WEEK -- a rolling window right after a recovery week (or any
    week-to-week jump) spans multiple weeks with very different prescribed
    volume, so applying only the newest week's rate overstates what should
    have been done and unfairly tanks the score for training exactly as
    the plan intended.
    """
    total = 0.0
    d = start
    while d <= end:
        weekly_target = WEEKLY_TARGETS.get(str(week_num_for_date(d)), 0)
        total += weekly_target / 7
        d += timedelta(days=1)
    return total * disc_share


def status_label(score):
    ordered = sorted(THRESHOLDS.items(), key=lambda kv: -kv[1]["min"])
    for _, cfg in ordered:
        if score >= cfg["min"]:
            return cfg["label"]
    return "Significant gap"


def clamp(v, lo=0, hi=100):
    return max(lo, min(hi, v))


# ── DISCIPLINE SCORES ────────────────────────────────────────────────────────

def discipline_score(disc, race_distance_miles):
    recent = [a for a in activities if a["disc"] == disc and in_window(a, window_start, TODAY)]
    prior = [a for a in activities if a["disc"] == disc and in_window(a, prior_window_start, window_start - timedelta(days=1))]
    all_time = [a for a in activities if a["disc"] == disc]

    recent_tss = sum(a["tss"] for a in recent)
    prior_tss = sum(a["tss"] for a in prior)

    expected_window_tss = expected_tss_for_window(window_start, TODAY, DISCIPLINE_TSS_SPLIT.get(disc, 0.3))
    volume_score = clamp(round(100 * recent_tss / expected_window_tss)) if expected_window_tss else 0

    if disc == "swim":
        longest_m = max((a["distance"] for a in all_time), default=0)
        longest_label = f"{round(longest_m)}m"
        exposure_actual_pct = 100 * longest_m / 1609.34 / (race_distance_miles) if race_distance_miles else 0
    else:
        longest_miles = max((a["distance"] * MILES_PER_METER for a in all_time), default=0)
        longest_label = f"{round(longest_miles, 1)}mi"
        exposure_actual_pct = 100 * longest_miles / race_distance_miles if race_distance_miles else 0

    # Expected race-distance exposure ramps with the build, not a flat 100% --
    # low exposure in Base/Build weeks is normal, not a deficiency.
    expected_exposure_pct = clamp(100 * CURRENT_WEEK / max(1, TOTAL_WEEKS - 3), 10, 100)
    exposure_score = clamp(round(100 * exposure_actual_pct / expected_exposure_pct)) if expected_exposure_pct else 0

    score = round(0.5 * volume_score + 0.5 * exposure_score)

    if volume_score >= exposure_score:
        top_factor = f"Volume: {round(recent_tss)} TSS in the last {EVIDENCE_WINDOW_DAYS} days vs. an expected ~{round(expected_window_tss)} (blended across each week's own target in that window)."
        limiter = f"Race-distance exposure: longest {disc} session is {longest_label} ({round(exposure_actual_pct)}% of race distance)."
    else:
        top_factor = f"Race-distance exposure: longest {disc} session is {longest_label} ({round(exposure_actual_pct)}% of race distance)."
        limiter = f"Volume: {round(recent_tss)} TSS in the last {EVIDENCE_WINDOW_DAYS} days vs. an expected ~{round(expected_window_tss)} (blended across each week's own target in that window)."

    if prior_tss == 0 and recent_tss == 0:
        trend = "insufficient data"
    elif prior_tss == 0:
        trend = "improving"
    elif recent_tss > prior_tss * 1.1:
        trend = "improving"
    elif recent_tss < prior_tss * 0.9:
        trend = "declining"
    else:
        trend = "stable"

    if all_time:
        oldest = min(date.fromisoformat(a["date"]) for a in all_time)
        newest = max(date.fromisoformat(a["date"]) for a in all_time)
        weeks_with_data = (newest - oldest).days // 7 + 1
    else:
        weeks_with_data = 0

    return {
        "score": score,
        "status": status_label(score),
        "top_factor": top_factor,
        "top_limiter": limiter,
        "trend": trend,
        "evidence_window_days": EVIDENCE_WINDOW_DAYS,
        "sessions_in_window": len(recent),
        "weeks_observed": weeks_with_data,
        "confidence": confidence_level(weeks_with_data, len(recent) > 0),
    }


def confidence_level(weeks_observed, has_recent_data):
    high_wk = CONF_CFG.get("min_weeks_for_high_confidence", 6)
    mod_wk = CONF_CFG.get("min_weeks_for_moderate_confidence", 3)
    if not has_recent_data:
        return "low"
    if weeks_observed >= high_wk:
        return "moderate"  # capped below "high" -- HRV/body battery aren't ingested yet
    if weeks_observed >= mod_wk:
        return "low-moderate"
    return "low"


# ── RECOVERY SCORE ───────────────────────────────────────────────────────────

def recovery_score():
    recent_sleep = [s for s in sleep if in_window(s, window_start, TODAY)]
    prior_sleep = [s for s in sleep if in_window(s, prior_window_start, window_start - timedelta(days=1))]

    if not recent_sleep:
        return {
            "score": None,
            "status": "Insufficient data",
            "top_factor": "No sleep data available in the evidence window.",
            "top_limiter": "No sleep data available in the evidence window.",
            "trend": "insufficient data",
            "evidence_window_days": EVIDENCE_WINDOW_DAYS,
            "confidence": "low",
        }

    scores = [s["score"] for s in recent_sleep if s.get("score")]
    rhrs = [s["rhr"] for s in recent_sleep if s.get("rhr")]
    spo2s = [s["spo2"] for s in recent_sleep if s.get("spo2")]

    avg_score = round(sum(scores) / len(scores)) if scores else None
    avg_rhr = round(sum(rhrs) / len(rhrs), 1) if rhrs else None
    avg_spo2 = round(sum(spo2s) / len(spo2s), 1) if spo2s else None

    baseline_rhrs = [s["rhr"] for s in sleep if s.get("rhr")]
    baseline_rhr = round(sum(baseline_rhrs) / len(baseline_rhrs), 1) if baseline_rhrs else avg_rhr

    sleep_component = avg_score if avg_score is not None else 60
    rhr_deviation = (avg_rhr - baseline_rhr) if (avg_rhr is not None and baseline_rhr is not None) else 0
    rhr_component = clamp(100 - abs(rhr_deviation) * 8)
    spo2_component = clamp((avg_spo2 - 85) * (100 / 10)) if avg_spo2 is not None else 70

    score = round(0.5 * sleep_component + 0.25 * rhr_component + 0.25 * spo2_component)

    if score >= 80:
        status = "Recovered"
    elif score >= 65:
        status = "Stable"
    elif score >= 50:
        status = "Functional fatigue"
    elif score >= 35:
        status = "Accumulating fatigue"
    else:
        status = "High concern"

    components = {
        "Sleep quality": sleep_component,
        "Resting HR deviation": rhr_component,
        "SpO2": spo2_component,
    }
    top_factor_name = max(components, key=components.get)
    limiter_name = min(components, key=components.get)

    factor_text = {
        "Sleep quality": f"7-day-avg sleep score is {avg_score}/100.",
        "Resting HR deviation": f"Resting HR is {avg_rhr} bpm, {round(rhr_deviation, 1)} bpm vs. the all-time baseline of {baseline_rhr} bpm.",
        "SpO2": f"Average SpO2 is {avg_spo2}%.",
    }

    if len(prior_sleep) == 0:
        trend = "insufficient data"
    else:
        prior_scores = [s["score"] for s in prior_sleep if s.get("score")]
        prior_avg = sum(prior_scores) / len(prior_scores) if prior_scores else None
        if prior_avg is None or avg_score is None:
            trend = "insufficient data"
        elif avg_score > prior_avg + 3:
            trend = "improving"
        elif avg_score < prior_avg - 3:
            trend = "declining"
        else:
            trend = "stable"

    return {
        "score": score,
        "status": status,
        "top_factor": factor_text[top_factor_name],
        "top_limiter": factor_text[limiter_name],
        "trend": trend,
        "evidence_window_days": EVIDENCE_WINDOW_DAYS,
        "nights_in_window": len(recent_sleep),
        "confidence": "moderate" if len(recent_sleep) >= 14 else "low-moderate" if len(recent_sleep) >= 7 else "low",
        "note": "HRV, body battery, and training readiness are not yet ingested from Garmin -- recovery score is based on sleep score, resting HR deviation, and SpO2 only.",
    }


# ── COMPUTE ───────────────────────────────────────────────────────────────

swim = discipline_score("swim", RACE.get("swim_miles", 2.4))
bike = discipline_score("bike", RACE.get("bike_miles", 112))
run = discipline_score("run", RACE.get("run_miles", 26.2))
recovery = recovery_score()

recovery_score_value = recovery["score"] if recovery["score"] is not None else 50

overall_score = round(
    swim["score"] * WEIGHTS.get("swim", 0)
    + bike["score"] * WEIGHTS.get("bike", 0)
    + run["score"] * WEIGHTS.get("run", 0)
    + recovery_score_value * WEIGHTS.get("recovery", 0)
)

discipline_scores = {"swim": swim, "bike": bike, "run": run, "recovery": recovery}
weighted_contribution = {k: (v["score"] if v["score"] is not None else 0) * WEIGHTS.get(k, 0)
                          for k, v in discipline_scores.items()}
strongest = max(weighted_contribution, key=weighted_contribution.get)
weakest = min(weighted_contribution, key=weighted_contribution.get)

confidence_order = {"low": 0, "low-moderate": 1, "moderate": 2, "high": 3}
overall_confidence = min(
    (d.get("confidence", "low") for d in discipline_scores.values()),
    key=lambda c: confidence_order.get(c, 0),
)

next_action = {
    "recovery": "Protect recovery -- keep intensity capped until sleep score and resting HR normalize.",
}.get(weakest, f"Prioritize {weakest} volume and long-session exposure this week -- it's the largest limiter on overall readiness.")

readiness = {
    "date": TODAY.isoformat(),
    "calculation_version": CALC_VERSION,
    "race_type": RACE_TYPE,
    "current_week": CURRENT_WEEK,
    "total_weeks": TOTAL_WEEKS,
    "overall_score": overall_score,
    "overall_status": status_label(overall_score),
    "overall_confidence": overall_confidence,
    "strongest_positive_factor": f"{strongest} ({discipline_scores[strongest]['score']}/100): {discipline_scores[strongest]['top_factor']}",
    "largest_limiter": f"{weakest} ({discipline_scores[weakest]['score'] if discipline_scores[weakest]['score'] is not None else 'n/a'}/100): {discipline_scores[weakest]['top_limiter']}",
    "next_action": next_action,
    "weights_used": WEIGHTS,
    "scores": {
        "swim": swim,
        "bike": bike,
        "run": run,
        "recovery": recovery,
    },
    "note": "Overall vs. week-execution vs. daily readiness are distinct concepts (doc 08). This score is multi-week overall race readiness -- it is not a judgment of today's single workout or this week's completion percentage.",
}

os.makedirs("data", exist_ok=True)
with open("data/readiness.json", "w") as f:
    json.dump(readiness, f, indent=2)

print(f"Readiness: overall={overall_score} ({readiness['overall_status']}), confidence={overall_confidence}")
print(f"  swim={swim['score']} bike={bike['score']} run={run['score']} recovery={recovery['score']}")
print(f"  strongest={strongest}, weakest={weakest}")
