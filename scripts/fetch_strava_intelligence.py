"""
fetch_strava_intelligence.py

Pulls recent activities from the Strava API (relative effort, splits, heart
rate, cadence, segment efforts -- everything Strava's own in-app "Athlete
Intelligence" card is built from, since that card's text itself isn't part
of the public API) and calls Claude to generate a short per-activity write-up
in a similar spirit, saved to data/activity_intelligence.json.

Only processes activities not already present in the output file, so this
is safe to run daily without re-spending Claude calls on old activities.
Strava credentials are optional, like the other integrations in this
pipeline -- if missing, this step is skipped and the existing file is left
untouched rather than failing the whole workflow.
"""

import json
import os
import sys
import urllib.request
import urllib.parse
import urllib.error
from datetime import timedelta

import datetime as _dt
PACIFIC = _dt.timezone(_dt.timedelta(hours=-7))
TODAY = _dt.datetime.now(PACIFIC).date()

STRAVA_CLIENT_ID     = os.environ.get("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET")
STRAVA_REFRESH_TOKEN = os.environ.get("STRAVA_REFRESH_TOKEN")
ANTHROPIC_API_KEY    = os.environ.get("ANTHROPIC_API_KEY")

LOOKBACK_DAYS = 5      # small buffer beyond 1 day so a late-syncing activity isn't missed
RETENTION_DAYS = 90    # trim entries older than this so the file doesn't grow unbounded

OUT_PATH = "data/activity_intelligence.json"


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def disc(sport_type):
    t = str(sport_type).upper()
    if any(x in t for x in ["SWIM"]): return "swim"
    if any(x in t for x in ["RIDE", "CYCLING", "VIRTUALRIDE", "BIKE", "MTB", "GRAVEL"]): return "bike"
    if any(x in t for x in ["RUN", "TRAIL", "TREADMILL", "WALK"]): return "run"
    return "other"


if not (STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET and STRAVA_REFRESH_TOKEN):
    print("No Strava credentials -- skipping activity intelligence fetch, leaving existing file untouched")
    sys.exit(0)

if not ANTHROPIC_API_KEY:
    print("No ANTHROPIC_API_KEY -- skipping activity intelligence fetch, leaving existing file untouched")
    sys.exit(0)


# ── REFRESH ACCESS TOKEN ─────────────────────────────────────────────────────
def get_access_token():
    payload = urllib.parse.urlencode({
        "client_id": STRAVA_CLIENT_ID,
        "client_secret": STRAVA_CLIENT_SECRET,
        "refresh_token": STRAVA_REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request("https://www.strava.com/oauth/token", data=payload, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())["access_token"]


def strava_get(path, token, params=None):
    url = f"https://www.strava.com/api/v3{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


try:
    access_token = get_access_token()
except Exception as e:
    print(f"Strava token refresh failed: {e}")
    sys.exit(0)  # don't fail the whole daily workflow over this integration

# ── RECENT ACTIVITIES ────────────────────────────────────────────────────────
after_epoch = int((_dt.datetime.combine(TODAY - timedelta(days=LOOKBACK_DAYS), _dt.time()) - _dt.datetime(1970, 1, 1)).total_seconds())

try:
    recent = strava_get("/athlete/activities", access_token, {"after": after_epoch, "per_page": 30})
except Exception as e:
    print(f"Strava activities fetch failed: {e}")
    sys.exit(0)

print(f"Found {len(recent)} Strava activities in the last {LOOKBACK_DAYS} days")

existing = load_json(OUT_PATH, [])
known_ids = {str(e.get("strava_id")) for e in existing}

# 30-day rolling pace context per discipline, from the existing Intervals-sourced
# activity log -- gives Claude something to compare each new activity against,
# the same way Strava's own card references "your 30-day average."
activities_log = load_json("data/activities.json", [])
window_start = (TODAY - timedelta(days=30)).isoformat()


def rolling_avg_pace_min_per_mi(d):
    acts = [a for a in activities_log if a["disc"] == d and a["date"] >= window_start and a.get("distance") and a.get("duration")]
    if not acts:
        return None
    paces = [(a["duration"] / 60) / (a["distance"] / 1609.34) for a in acts if a["distance"] > 0]
    return round(sum(paces) / len(paces), 2) if paces else None


def rolling_avg_pace_per_100yd():
    """Swim pace convention is per-100yd, not per-mile -- a mile of pool
    swimming is a meaningless training reference point for this athlete."""
    acts = [a for a in activities_log if a["disc"] == "swim" and a["date"] >= window_start and a.get("distance") and a.get("duration")]
    if not acts:
        return None
    paces = [(a["duration"] / 60) / (a["distance"] / 0.9144 / 100) for a in acts if a["distance"] > 0]
    return round(sum(paces) / len(paces), 2) if paces else None


def fmt_minsec(total_minutes):
    """Convert decimal minutes (e.g. 15.97) to M:SS (e.g. 15:58). Feeding the
    model a raw decimal-minutes number invites it to misread the fractional
    digits as literal seconds (15.97 -> "15:97", an impossible pace) -- doing
    the conversion here removes that ambiguity entirely."""
    if total_minutes is None:
        return None
    m = int(total_minutes)
    s = round((total_minutes - m) * 60)
    if s == 60:
        m += 1
        s = 0
    return f"{m}:{s:02d}"


def fmt_pace(min_per_unit, suffix="/mi"):
    ms = fmt_minsec(min_per_unit)
    return f"{ms}{suffix}" if ms else None


# ── CLAUDE CALL (same pattern as analyze_training.py) ────────────────────────
def generate_writeup(facts_text):
    prompt = f"""You are generating a short "Activity Intelligence" write-up for a triathlete's workout, in the same spirit as Strava's own AI activity summaries: a bold one-sentence headline, then a short 2-3 sentence paragraph of specific, data-driven analysis. Encouraging coach voice, not generic praise -- reference the actual numbers given.

IMPORTANT: only use the units and terms actually present in the data below -- never convert or restate a figure in a different unit than given (e.g. never describe a swim in "per mile" pace or "miles" -- swim distance is in yards and pace is per 100yd; never invent "mile splits" for a swim, since none are provided).

Activity data:
{facts_text}

Respond with valid JSON only, no other text, in this exact shape:
{{"headline": "one bold sentence, no markdown formatting", "analysis": "2-3 sentences of specific analysis referencing the numbers above"}}"""

    payload = json.dumps({
        "model": "claude-sonnet-4-5",
        "max_tokens": 400,
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
    with urllib.request.urlopen(req) as response:
        result = json.loads(response.read().decode())
        text = result["content"][0]["text"].strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())


new_entries = []
for a in recent:
    sid = str(a["id"])
    if sid in known_ids:
        continue

    d = disc(a.get("sport_type", ""))
    date_local = str(a.get("start_date_local", ""))[:10]

    try:
        detail = strava_get(f"/activities/{sid}", access_token)
    except Exception as e:
        print(f"  Skipping {sid} ({a.get('name')}) -- detail fetch failed: {e}")
        continue

    if d == "swim":
        # Strava's GPS-based mile splits and route segments are unreliable
        # for an indoor pool swim (no real GPS track) -- the mile-marker
        # splits/segments have previously produced nonsense like a "mile"
        # taking 49 minutes then the next taking 12. Omit them rather than
        # feed the model data that will mislead it into run-style "per mile"
        # commentary for a pool swim.
        split_lines = []
        segment_lines = []
        avg_pace = rolling_avg_pace_per_100yd()
        pace_fact = f"- Athlete's 30-day average pace for swim: {fmt_pace(avg_pace, '/100yd') or 'not enough data'}"
        cadence_fact = f"- Average stroke rate: {detail.get('average_cadence', 'not available')} (strokes/min)"
    else:
        splits = detail.get("splits_standard") or []
        split_lines = [
            f"  mile {s.get('split')}: {fmt_minsec(s.get('elapsed_time', 0) / 60)} min/mi, avg HR {s.get('average_heartrate', '—')}"
            for s in splits
        ]
        segments = detail.get("segment_efforts") or []
        segment_lines = [
            f"  {seg.get('name')}: {seg.get('elapsed_time')}s"
            for seg in segments[:5]
        ]
        avg_pace = rolling_avg_pace_min_per_mi(d)
        pace_fact = f"- Athlete's 30-day average pace for {d}: {fmt_pace(avg_pace) or 'not enough data'}"
        cadence_fact = f"- Average {'pedal cadence (RPM)' if d == 'bike' else 'run cadence (steps/min)'}: {detail.get('average_cadence', 'not available')}"

    facts = "\n".join([
        f"- Title: {a.get('name')}",
        f"- Discipline: {d}",
        f"- Distance: {round(a.get('distance', 0) / 1609.34, 2)} miles" if d != "swim" else f"- Distance: {round(a.get('distance', 0) / 0.9144)} yards",
        f"- Moving time: {round(a.get('moving_time', 0) / 60, 1)} minutes",
        f"- Relative effort (suffer score): {detail.get('suffer_score', 'not available')}",
        f"- Average heart rate: {detail.get('average_heartrate', 'not available')}",
        f"- Max heart rate: {detail.get('max_heartrate', 'not available')}",
        cadence_fact,
        f"- Kudos: {a.get('kudos_count', 0)}, PRs set: {a.get('pr_count', 0)}",
        pace_fact,
        "- Mile splits:\n" + "\n".join(split_lines) if split_lines else "- No split data available",
        "- Notable segments:\n" + "\n".join(segment_lines) if segment_lines else "",
    ])

    try:
        writeup = generate_writeup(facts)
    except Exception as e:
        print(f"  Skipping {sid} ({a.get('name')}) -- Claude generation failed: {e}")
        continue

    new_entries.append({
        "strava_id": sid,
        "date": date_local,
        "disc": d,
        "title": a.get("name"),
        "headline": writeup.get("headline"),
        "analysis": writeup.get("analysis"),
        "relative_effort": detail.get("suffer_score"),
    })
    print(f"  Generated write-up for {sid}: {a.get('name')}")

merged = existing + new_entries
cutoff = (TODAY - timedelta(days=RETENTION_DAYS)).isoformat()
merged = [e for e in merged if e.get("date", "") >= cutoff]
merged.sort(key=lambda e: e["date"])

os.makedirs("data", exist_ok=True)
with open(OUT_PATH, "w") as f:
    json.dump(merged, f, indent=2)

print(f"Saved {len(merged)} activity write-ups ({len(new_entries)} new)")
