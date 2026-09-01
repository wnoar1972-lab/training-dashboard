"""
analyze_training.py
Reads latest Garmin/Intervals.icu data plus the readiness engine output and
calls Claude to generate a daily coaching analysis, saved to
data/analysis.json.

Athlete profile, race configuration, and the training schedule are loaded
from config/*.json (single sources of truth, doc 05/02/13) rather than
hardcoded here -- this script only builds the prompt and calls the model.
"""

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import date, timedelta

from schedule_lib import load_schedule, resolve_day, resolve_week, day_label

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
if not ANTHROPIC_API_KEY:
    print("ERROR: Set ANTHROPIC_API_KEY environment variable")
    sys.exit(1)

import datetime as _dt
TODAY = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=-7))).date()
DAY_OF_WEEK = TODAY.strftime('%A')
DAYS_INTO_WEEK = (TODAY.weekday() + 1) % 7  # Mon=1 ... Sun=7
if DAYS_INTO_WEEK == 0: DAYS_INTO_WEEK = 7
DAYS_LEFT_IN_WEEK = 7 - DAYS_INTO_WEEK


# ── LOAD DATA + CONFIG ────────────────────────────────────────────────────────
def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        print(f"Warning: could not load {path}: {e}")
        return default if default is not None else {}


activities = load_json("data/activities.json", [])
sleep_data = load_json("data/sleep.json", [])
summary = load_json("data/summary.json", {})
readiness = load_json("data/readiness.json", None)
overall = summary.get("overall", {})
weeks = summary.get("weeks", [])

PROFILE = load_json("config/athlete_profile.json", {})
RACE = load_json("config/race_config.json", {})
SCHEDULE = load_schedule()

WEEK_TARGETS = {int(k): v for k, v in RACE.get("weekly_tss_targets", {}).items()}
WEEK_PHASES = RACE.get("week_phases", {})
WEEK_FOCUS = RACE.get("week_focus", {})

# ── RESOLVE THIS WEEK'S SCHEDULE FROM THE SINGLE SOURCE OF TRUTH ────────────
_this_monday_date = TODAY - timedelta(days=TODAY.weekday())
this_week_schedule = resolve_week(SCHEDULE, _this_monday_date)
today_planned = day_label(resolve_day(SCHEDULE, TODAY))
schedule_line = ", ".join(f"{d[:3]}={s}" for d, s in this_week_schedule.items())

# ── FIND YESTERDAY AND RECENT WORKOUTS ───────────────────────────────────────
yesterday = (TODAY - timedelta(days=1)).strftime("%Y-%m-%d")
last7days = [(TODAY - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]

yesterday_acts = [a for a in activities if a["date"] == yesterday]
last7_acts     = [a for a in activities if a["date"] in last7days]
last7_sleep    = [s for s in sleep_data if s["date"] in last7days]

current_week = overall.get("currentWeek", 1)
current_week_data = next((w for w in weeks if w["week"] == current_week), {})
week_target  = WEEK_TARGETS.get(current_week, 380)
week_actual  = current_week_data.get("actual", 0)
week_pct     = round(week_actual / week_target * 100) if week_target else 0

total_bike = len([a for a in activities if a["disc"] == "bike"])
total_swim = len([a for a in activities if a["disc"] == "swim"])
total_run  = len([a for a in activities if a["disc"] == "run"])
total_bike_tss = round(sum(a["tss"] for a in activities if a["disc"] == "bike"))

from collections import defaultdict
day_discs = defaultdict(set)
for a in activities:
    day_discs[a["date"]].add(a["disc"])
brick_days = [d for d, discs in day_discs.items() if "bike" in discs and "run" in discs]

recent_spo2   = [s["spo2"] for s in last7_sleep if s["spo2"] > 0]
recent_scores = [s["score"] for s in last7_sleep if s["score"] > 0]
avg_spo2  = round(sum(recent_spo2)/len(recent_spo2), 1) if recent_spo2 else 0
avg_score = round(sum(recent_scores)/len(recent_scores)) if recent_scores else 0
low_spo2_nights = len([s for s in recent_spo2 if s < 92])


# ── BUILD PROMPT SECTIONS FROM CONFIG (not hardcoded prose) ─────────────────

def build_athlete_context_text():
    identity = PROFILE.get("identity", {})
    lines = [f"- Age: {identity.get('age', 'unknown')} years old"]

    race_history = PROFILE.get("athletic_background", {}).get("race_history", [])
    for r in race_history:
        splits = r.get("splits")
        splits_text = ""
        if splits:
            splits_text = " | " + " | ".join(f"{k.upper()} {v}" for k, v in splits.items())
        lines.append(f"- {r.get('name')} ({r.get('date')}): {r.get('result')}{splits_text}")
        for lesson in r.get("lessons", []):
            lines.append(f"  - Lesson: {lesson}")

    for m in PROFILE.get("medical_considerations", []):
        lines.append(f"- Medical consideration ({m.get('category')}): {m.get('description')}")
        for g in m.get("coaching_guardrails", []):
            lines.append(f"  - Guardrail: {g}")

    for s in PROFILE.get("supplements", []):
        lines.append(f"- {s.get('name')}: {s.get('purpose')} ({s.get('dosage_timing')})")

    for e in PROFILE.get("equipment", []):
        lines.append(f"- Equipment: {e.get('name')} -- {e.get('fit_notes')}")

    prefs = PROFILE.get("coaching_preferences", {})
    if prefs.get("philosophy"):
        lines.append(f"- Coaching philosophy: {prefs['philosophy']}")
    if prefs.get("walk_run_protocol"):
        lines.append(f"- Walk/run protocol: {prefs['walk_run_protocol']}")

    return "\n".join(lines)


def build_race_targets_text():
    splits = RACE.get("target_splits", {})
    strategy = RACE.get("strategy", {})
    lines = [
        f"- Race: {RACE.get('name')}, {RACE.get('location')}, {RACE.get('date')} "
        f"({overall.get('daysToRace', '?')} days away)",
        f"- Distances: {RACE.get('swim_miles')}mi swim / {RACE.get('bike_miles')}mi bike / {RACE.get('run_miles')}mi run",
        f"- Goal finish: {RACE.get('goal_finish')}",
        f"- Swim target: {splits.get('swim')}",
        f"- Bike target: {splits.get('bike')} ({strategy.get('bike_target_power_note', '')})",
        f"- Run target: {splits.get('run')} at {strategy.get('run_target_pace')}, walk/run {strategy.get('run_walk_strategy')}",
    ]
    lessons = RACE.get("prior_race_lessons_carried_forward", [])
    if lessons:
        lines.append("- Key fixes carried forward from the prior race: " + "; ".join(lessons))
    return "\n".join(lines)


def build_readiness_evidence_text():
    if not readiness:
        return "No readiness engine output available yet."
    s = readiness["scores"]
    return (
        f"- Overall readiness: {readiness['overall_score']}/100 ({readiness['overall_status']}), "
        f"confidence {readiness['overall_confidence']}\n"
        f"- Swim {s['swim']['score']}/100, Bike {s['bike']['score']}/100, Run {s['run']['score']}/100, "
        f"Recovery {s['recovery']['score']}/100\n"
        f"- Strongest factor: {readiness['strongest_positive_factor']}\n"
        f"- Largest limiter: {readiness['largest_limiter']}"
    )


athlete_context_text = build_athlete_context_text()
race_targets_text = build_race_targets_text()
readiness_evidence_text = build_readiness_evidence_text()

def scheduled_for(date_str):
    """What was actually scheduled on this date -- may belong to a different
    build week than today's, with different targets (e.g. a recovery week's
    easy session vs. a peak week's key session). Always attach this so the
    model grades each day against its OWN plan, not today's week's targets."""
    d = date.fromisoformat(date_str)
    return day_label(resolve_day(SCHEDULE, d))

def format_distance(a):
    """Swim targets in the schedule are always stated in meters (e.g. "Pool
    swim 3,200m"); bike/run targets are always in miles. Show the actual
    distance in whichever unit that discipline's targets use, so the model
    never has to mentally convert between them when comparing actual vs.
    scheduled -- that conversion step is exactly where it can go wrong
    (e.g. reading "2.0mi" against a "3,200m" target as if they were
    different distances, when 2.0mi is essentially 3,200m)."""
    if not a["distance"]:
        return "0"
    if a["disc"] == "swim":
        return f"{round(a['distance'])}m"
    return f"{round(a['distance'] / 1609.34, 1)}mi"


yesterday_summary = ""
if yesterday_acts:
    for a in yesterday_acts:
        yesterday_summary += f"- {a['title']} ({a['disc']}) | TSS: {a['tss']} | HR: {a['avgHR']} | Distance: {format_distance(a)} | Scheduled that day: {scheduled_for(yesterday)}\n"
else:
    yesterday_summary = f"- Rest day (no activities logged) | Scheduled that day: {scheduled_for(yesterday)}"

last7_summary = ""
for a in sorted(last7_acts, key=lambda x: x["date"], reverse=True):
    last7_summary += f"- {a['date']} | {a['title']} ({a['disc']}) | TSS: {a['tss']} | HR: {a['avgHR']} | {format_distance(a)} | Scheduled that day: {scheduled_for(a['date'])}\n"

sleep_summary = ""
for s in sorted(last7_sleep, key=lambda x: x["date"], reverse=True):
    sleep_summary += f"- {s['date']} | Score: {s['score']} | Duration: {s['total']}h | SpO2: {s['spo2']}% | RHR: {s['rhr']} | Deep: {s['deep']}min | REM: {s['rem']}min\n"

week1_rule = RACE.get("week1_special_rule", "")

prompt = f"""You are an expert Ironman triathlon coach analyzing an athlete's daily training data.

COACHING STYLE: Be direct, honest, and specific. Do not sugar-coat. Do not over-encourage. If the athlete is behind on training, say so clearly. If a session was weak, say so. If something is a red flag for race readiness, name it directly. This athlete needs accurate feedback to be ready for a full Ironman -- false reassurance is more harmful than hard truths. At the same time, acknowledge genuine progress when it is earned.

ATHLETE PROFILE:
{athlete_context_text}

RACE TARGETS:
{race_targets_text}

READINESS ENGINE OUTPUT (evidence, not something to restate verbatim -- interpret it):
{readiness_evidence_text}

CURRENT TRAINING STATUS:
- Today is {DAY_OF_WEEK}. {DAYS_LEFT_IN_WEEK} day(s) remain in this training week, including today.
- Build: WEEK {current_week} OF {RACE.get('build', {}).get('total_weeks', 13)}. Week focus: {WEEK_FOCUS.get(str(current_week), '')}
- Week TSS so far: {week_actual} of {week_target} target ({week_pct}% complete). Weekly TSS is typically back-loaded toward the long ride/run and any midweek brick, so a low percentage earlier in the week is often normal -- only flag the week as behind if a scheduled session was actually skipped, or it is Thursday or later and the remaining days realistically cannot reach the target.
- {week1_rule}
- This week's schedule: {schedule_line}
- TODAY ({DAY_OF_WEEK}) planned session: {today_planned}
- Total activities in build: {overall.get('totalActs', 0)} ({total_bike} bike, {total_swim} swim, {total_run} run)
- Total bike TSS: {total_bike_tss}
- Brick workouts completed: {len(brick_days)} (dates: {', '.join(brick_days) if brick_days else 'none yet'})

YESTERDAY'S WORKOUT ({yesterday}):
{yesterday_summary}

LAST 7 DAYS OF TRAINING:
{last7_summary if last7_summary else 'No activities in last 7 days'}

LAST 7 NIGHTS SLEEP:
{sleep_summary if sleep_summary else 'No sleep data available'}
7-day avg sleep score: {avg_score}/100
7-day avg SpO2: {avg_spo2}%
Nights below 92% SpO2: {low_spo2_nights} (note: may be affected by night sweating)

When writing todayRecommendation, base it on the TODAY planned session listed above -- do not assume a different workout. Never invent data that isn't provided above; if evidence is thin, say so directly in confidence.reason rather than filling the gap with a guess.

IMPORTANT -- grading past days: "This week's schedule" and the current week's TSS target above apply ONLY to the CURRENT week (Week {current_week}). Entries in YESTERDAY'S WORKOUT and LAST 7 DAYS OF TRAINING each carry their own "Scheduled that day" label -- a day may fall in a DIFFERENT build week (e.g. a recovery week) with completely different targets than the current week. Always grade a completed activity against ITS OWN "Scheduled that day" label, never against the current week's Saturday/Thursday/etc. targets if that activity happened on a different date in a different week. Do not describe a past easy/recovery session as "missing" or "shortened" relative to a big session (like a peak-week long ride) that is scheduled for a later date and has not happened yet.

IMPORTANT -- comparing distances: actual swim distances above are shown in meters and actual bike/run distances in miles, matching whichever unit that discipline's scheduled targets use (e.g. "Pool swim 3,200m"). When a distance is within a small margin of its scheduled target in that same unit, that means the target was met -- do not describe it as "short of" the target.

Please provide a structured daily coaching analysis in JSON format with exactly these fields:

{{
  "date": "{TODAY.strftime('%B %d, %Y')}",
  "overallStatus": "one of: On Track / Needs Attention / Great Week / Recovery Mode",
  "statusColor": "one of: green / yellow / red / blue",
  "yesterdayAnalysis": "2-3 sentences analyzing yesterday's workout",
  "todayRecommendation": "1-2 sentences on what today's training should focus on",
  "weekProgress": "1-2 sentences on how the week is tracking vs targets. Always describe the current week as 'Week {current_week} of {RACE.get('build', {}).get('total_weeks', 13)}'.",
  "keyInsight": "1 sentence -- the single most important coaching observation right now",
  "alerts": ["array of short alert strings if anything needs attention -- empty array if all good"],
  "positives": ["array of 2-3 short positive observations from recent training"],
  "recoveryScore": number from 1-10 based on sleep quality and training load (10 = fully recovered),
  "trainingLoadStatus": "one of: Fresh / Optimal / Fatigued / Overreached",
  "evidence": ["array of short strings naming the specific data sources used, e.g. 'Readiness engine (run 86/100)', 'Garmin sleep', 'Intervals.icu planned vs actual'"],
  "confidence": {{
    "level": "one of: low / moderate / high",
    "reason": "1 sentence on why -- cite data completeness/freshness/weeks observed, and name anything not yet tracked (e.g. HRV, nutrition log) that limits confidence"
  }},
  "athlete_context_considered": ["array of short strings naming which athlete-profile facts shaped this recommendation, e.g. 'Hip history', 'Active race', 'Walk/run protocol'"],
  "race_implications": ["array of 1-3 short strings on what this means for the race goal specifically"],
  "recommended_adjustments": ["array of 1-3 concrete adjustments to make, if any -- empty array if none needed"]
}}

Be specific and data-driven. Reference actual numbers from the data. Respond with valid JSON only, no other text."""


# ── CALL CLAUDE API ───────────────────────────────────────────────────────────
print(f"Calling Claude API for training analysis ({TODAY})...")

payload = json.dumps({
    "model": "claude-sonnet-4-5",
    "max_tokens": 1400,
    "messages": [{"role": "user", "content": prompt}]
}).encode()

req = urllib.request.Request(
    "https://api.anthropic.com/v1/messages",
    data=payload,
    headers={
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01"
    },
    method="POST"
)


def fallback_analysis(reason):
    return {
        "date": TODAY.strftime("%B %d, %Y"),
        "overallStatus": "On Track",
        "statusColor": "green",
        "yesterdayAnalysis": "Analysis temporarily unavailable -- check back tomorrow.",
        "todayRecommendation": f"Follow today's scheduled session: {today_planned}.",
        "weekProgress": f"Week {current_week} of {RACE.get('build', {}).get('total_weeks', 13)} -- {week_actual} of {week_target} TSS ({week_pct}% complete).",
        "keyInsight": f"{overall.get('daysToRace', '?')} days to {RACE.get('name', 'race day')}.",
        "alerts": [reason],
        "positives": ["Training is progressing", "Consistency is key"],
        "recoveryScore": readiness["scores"]["recovery"]["score"] // 10 if readiness and readiness["scores"]["recovery"]["score"] else 7,
        "trainingLoadStatus": "Optimal",
        "evidence": ["Fallback -- Claude API unavailable"],
        "confidence": {"level": "low", "reason": reason},
        "athlete_context_considered": [],
        "race_implications": [],
        "recommended_adjustments": [],
        "generatedAt": TODAY.strftime("%Y-%m-%d"),
        "daysToRace": overall.get("daysToRace", None),
        "currentWeek": current_week,
        "weekPhase": WEEK_PHASES.get(str(current_week), "Build"),
    }


try:
    with urllib.request.urlopen(req) as response:
        result = json.loads(response.read().decode())
        text = result["content"][0]["text"].strip()

        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        text = text.strip()

        analysis = json.loads(text)
        analysis["generatedAt"] = TODAY.strftime("%Y-%m-%d")
        analysis["daysToRace"]  = overall.get("daysToRace", None)
        analysis["currentWeek"] = current_week
        analysis["weekPhase"]   = WEEK_PHASES.get(str(current_week), "Build")

        with open("data/analysis.json", "w") as f:
            json.dump(analysis, f, indent=2)

        print("Analysis saved successfully")
        print(f"Status: {analysis.get('overallStatus')}")
        print(f"Key insight: {analysis.get('keyInsight')}")

except urllib.error.HTTPError as e:
    error_body = e.read().decode()
    print(f"Claude API error {e.code}: {error_body}")
    with open("data/analysis.json", "w") as f:
        json.dump(fallback_analysis(f"Claude API error {e.code} -- see workflow logs."), f, indent=2)
    print("Saved fallback analysis")

except Exception as e:
    print(f"Unexpected error: {e}")
    import traceback; traceback.print_exc()
    with open("data/analysis.json", "w") as f:
        json.dump(fallback_analysis(f"Unexpected error generating analysis: {e}"), f, indent=2)
    print("Saved fallback analysis")

print("Analysis complete")
