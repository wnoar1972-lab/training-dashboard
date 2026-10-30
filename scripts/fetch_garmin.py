"""
fetch_garmin.py
Pulls activities from Intervals.icu (for proper TSS across all disciplines)
and sleep data from Garmin Connect, saves to data/ folder.
"""

import json, os, sys, urllib.request, urllib.parse, base64, subprocess
subprocess.run([sys.executable, '-m', 'pip', 'install', 'requests', '-q'], check=False)
import requests
from datetime import date, timedelta

import datetime as _dt
TODAY = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=-7))).date()

with open("config/race_config.json") as f:
    RACE_CONFIG = json.load(f)

START_DATE = date.fromisoformat(RACE_CONFIG["build"]["start_date"])
TOTAL_WEEKS = RACE_CONFIG["build"]["total_weeks"]
WEEKLY_TSS_TARGETS = {int(k): v for k, v in RACE_CONFIG["weekly_tss_targets"].items()}
RACE_DATE = date.fromisoformat(RACE_CONFIG["date"])

# The day-by-day schedule (config/schedule.json) resolves every date to the
# Monday of its real calendar week (Mon-Sun), even for dates right after a
# race where the override happens to be keyed a day later. TSS week windows
# must snap to that same Monday-anchored calendar week -- anchoring strictly
# to START_DATE itself (which can fall on any weekday) would silently drift
# the TSS windows a day out of sync with what the schedule actually plans
# for that week, once the build reaches a week whose Monday differs from
# START_DATE's weekday. Week 13 lands exactly on RACE_DATE's Sunday.
WEEK_ANCHOR = START_DATE - timedelta(days=START_DATE.weekday())

# Activities are pulled from 60 days back (matching sleep) so sessions logged
# before the build starts still show up. Weekly TSS only counts build weeks.
ACTIVITY_START = min(START_DATE, TODAY - timedelta(days=60))

# ── CREDENTIALS ───────────────────────────────────────────────────────────────
GARMIN_EMAIL    = os.environ.get("GARMIN_EMAIL")
GARMIN_PASSWORD = os.environ.get("GARMIN_PASSWORD")
INTERVALS_KEY   = os.environ.get("INTERVALS_API_KEY")
INTERVALS_ID    = os.environ.get("INTERVALS_ATHLETE_ID")

os.makedirs("data", exist_ok=True)

def disc(t):
    t = str(t).upper()
    if any(x in t for x in ["SWIM","POOL","OPEN_WATER","OPENWATER"]): return "swim"
    if any(x in t for x in ["RIDE","CYCLING","VIRTUAL","BIKE","ROUVY",
                              "VELODROME","INDOOR","VIRTUALRIDE","CYCLE",
                              "EBIKE","MTB","ROAD","GRAVEL"]): return "bike"
    if any(x in t for x in ["RUN","RUNNING","TRAIL","TREADMILL","WALK"]): return "run"
    return "other"

def sf(v, d=0):
    try: return round(float(v), 1) if v else d
    except: return d

# ── INTERVALS.ICU ACTIVITIES ──────────────────────────────────────────────────
activities = []
if INTERVALS_KEY and INTERVALS_ID:
    try:
        print(f"Fetching activities from Intervals.icu ({ACTIVITY_START} to {TODAY})...")
        url = (f"https://intervals.icu/api/v1/athlete/{INTERVALS_ID}/activities"
               f"?oldest={ACTIVITY_START}&newest={TODAY}&cols=name,type,start_date_local,"
               f"distance,moving_time,tss,normalized_power,average_heartrate,icu_training_load,load")
        resp = requests.get(url, auth=("API_KEY", INTERVALS_KEY))
        print(f"Intervals.icu status: {resp.status_code}")
        resp.raise_for_status()
        raw = resp.json()
        print(f"Got {len(raw)} activities from Intervals.icu")

        # Debug — print all activity types so we can see what ROUVY sends
        print("=== ACTIVITY TYPES ===")
        for a in raw:
            print(f"  {a.get('start_date_local','')[:10]} | type={a.get('type','?')} | name={str(a.get('name',''))[:30]} | tss={a.get('tss')} | load={a.get('icu_training_load')}")
        print("=== END TYPES ===")

        for a in raw:
            act_type = a.get("type", "")
            d = disc(act_type)
            tss = sf(a.get("tss") or a.get("icu_training_load") or a.get("load"))
            np  = sf(a.get("normalized_power"))
            dt  = str(a.get("start_date_local",""))[:10]
            activities.append({
                "date":     dt,
                "title":    str(a.get("name","Activity"))[:40],
                "type":     act_type[:8],
                "disc":     d,
                "tss":      tss,
                "np":       np,
                "avgHR":    sf(a.get("average_heartrate")),
                "distance": sf(a.get("distance")),
                "duration": sf(a.get("moving_time")),
            })
        activities.sort(key=lambda x: x["date"])
        with open("data/activities.json","w") as f:
            json.dump(activities, f, indent=2)
        print(f"Saved {len(activities)} activities with full TSS")
        for d_type in ["swim","bike","run","other"]:
            acts = [a for a in activities if a["disc"]==d_type]
            total_tss = sum(a["tss"] for a in acts)
            print(f"  {d_type}: {len(acts)} sessions, {total_tss:.0f} TSS")

    except Exception as e:
        print(f"Intervals.icu error: {e}")
        import traceback; traceback.print_exc()
else:
    print("No Intervals.icu credentials — skipping activity fetch, reusing existing data/activities.json")
    try:
        with open("data/activities.json") as f:
            activities = json.load(f)
    except Exception:
        activities = []

# ── GARMIN SLEEP ──────────────────────────────────────────────────────────────
sleep_data = []
if GARMIN_EMAIL and GARMIN_PASSWORD:
    try:
        from garminconnect import Garmin
        print("Logging in to Garmin Connect for sleep data...")
        client = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
        client.login()
        print("Logged in")

        def quality(score):
            if score>=80: return "Good"
            if score>=60: return "Fair"
            if score>0:   return "Poor"
            return "—"

        cur = TODAY - timedelta(days=60)
        while cur <= TODAY:
            ds = cur.strftime("%Y-%m-%d")
            try:
                raw = client.get_sleep_data(ds)
                if raw and isinstance(raw, dict):
                    dto = raw.get("dailySleepDTO") or {}
                    score = 0
                    quality_key = ""
                    # Primary: sleepScores.overall.value (confirmed field from Garmin)
                    sleep_scores = dto.get("sleepScores") or raw.get("sleepScores")
                    if sleep_scores and isinstance(sleep_scores, dict):
                        overall = sleep_scores.get("overall")
                        if overall and isinstance(overall, dict):
                            v = overall.get("value")
                            if v: score = int(v)
                            quality_key = str(overall.get("qualifierKey","")).upper()
                    # Fallback
                    if not score:
                        for src in [dto, raw]:
                            for k in ["overallSleepScore","sleepScore"]:
                                v = src.get(k)
                                if isinstance(v,(int,float)) and v>0: score=int(v); break
                            if score: break
                    spo2 = 0
                    for src in [dto, raw]:
                        for k in ["averageSpO2Value","avgSPO2","spo2","averageSpo2"]:
                            v = src.get(k)
                            if v:
                                try: spo2=round(float(v),1); break
                                except: pass
                        if spo2: break
                    if not spo2:
                        s2 = raw.get("spo2SleepSummary") or {}
                        for k in ["averageSPO2","avg","average","averageValue"]:
                            v = s2.get(k)
                            if v:
                                try: spo2=round(float(v),1); break
                                except: pass
                    if not spo2:
                        ep = raw.get("wellnessEpochSPO2DataDTOList",[]) or []
                        vals=[x.get("spo2Reading",0) for x in ep if x.get("spo2Reading")]
                        if vals: spo2=round(sum(vals)/len(vals),1)
                    rhr = 0
                    for src in [dto, raw]:
                        for k in ["restingHeartRate","avgRestingHeartRate","averageRestingHeartRate"]:
                            v = src.get(k)
                            if v:
                                try: rhr=int(v); break
                                except: pass
                        if rhr: break
                    # Average stress while asleep, plus Garmin's rating for each sleep-score contributor
                    stress_avg = 0
                    for src in [dto, raw]:
                        v = src.get("avgSleepStress")
                        if v:
                            try: stress_avg=round(float(v),1); break
                            except: pass
                    contributors = {}
                    if sleep_scores and isinstance(sleep_scores, dict):
                        for name, c in sleep_scores.items():
                            if name=="overall" or not isinstance(c, dict): continue
                            contributors[name] = {
                                "value":  c.get("value"),
                                "rating": str(c.get("qualifierKey","")).title()
                            }
                    total = int(dto.get("sleepTimeSeconds") or dto.get("totalSleepSeconds") or 0)
                    deep  = int(dto.get("deepSleepSeconds") or 0)
                    rem   = int(dto.get("remSleepSeconds") or 0)
                    # Use qualifierKey if available, otherwise derive from score
                    if quality_key in ["EXCELLENT","GOOD"]:
                        qual = "Good"
                    elif quality_key in ["FAIR"]:
                        qual = "Fair"
                    elif quality_key in ["POOR","UNKNOWN"]:
                        qual = "Poor"
                    else:
                        qual = quality(score)
                    if total > 3600 or spo2 > 0:
                        sleep_data.append({
                            "date":    ds,
                            "score":   score,
                            "total":   round(total/3600,1),
                            "deep":    round(deep/60),
                            "rem":     round(rem/60),
                            "spo2":    spo2,
                            "rhr":     rhr,
                            "stress":  stress_avg,
                            "contributors": contributors,
                            "quality": qual
                        })
            except Exception: pass
            cur += timedelta(days=1)

        sleep_data.sort(key=lambda x:x["date"])
        with open("data/sleep.json","w") as f:
            json.dump(sleep_data, f, indent=2)
        print(f"Saved {len(sleep_data)} sleep nights")

    except Exception as e:
        print(f"Garmin sleep error: {e}")
        import traceback; traceback.print_exc()

# ── SUMMARY ───────────────────────────────────────────────────────────────────
# Week boundaries are derived from WEEK_ANCHOR (see above) -- not hardcoded
# here -- so the build calendar has a single source of truth shared with the
# day-by-day schedule.
week_ranges = []
for wn in range(1, TOTAL_WEEKS + 1):
    ws = (WEEK_ANCHOR + timedelta(days=(wn - 1) * 7))
    we = ws + timedelta(days=6)
    week_ranges.append((wn, ws.strftime("%Y-%m-%d"), we.strftime("%Y-%m-%d")))

weeks = []
for wn, ws, we in week_ranges:
    wa = [a for a in activities if ws <= a["date"] <= we]
    bn = [a for a in wa if a["disc"]=="bike" and a["np"]>0]
    swim_tss = round(sum(a["tss"] for a in wa if a["disc"]=="swim"))
    bike_tss = round(sum(a["tss"] for a in wa if a["disc"]=="bike"))
    run_tss  = round(sum(a["tss"] for a in wa if a["disc"]=="run"))
    total_tss = round(sum(a["tss"] for a in wa))
    weeks.append({
        "week":wn,"start":ws,"end":we,"target":WEEKLY_TSS_TARGETS.get(wn, 0),
        "actual":total_tss,
        "bikeTSS":bike_tss,
        "swimTSS":swim_tss,
        "runTSS":run_tss,
        "swimN":len([a for a in wa if a["disc"]=="swim"]),
        "bikeN":len([a for a in wa if a["disc"]=="bike"]),
        "runN":len([a for a in wa if a["disc"]=="run"]),
        "avgNP":round(sum(a["np"] for a in bn)/len(bn),1) if bn else 0,
        "done":we < TODAY.strftime("%Y-%m-%d")
    })

bn_all = [a["np"] for a in activities if a["disc"]=="bike" and a["np"]>0]
overall = {
    "lastUpdated":   TODAY.strftime("%B %d, %Y"),
    "totalActs":     len(activities),
    "avgBikeNP":     round(sum(bn_all)/len(bn_all),1) if bn_all else 0,
    "currentWeek":   next((w["week"] for w in weeks if not w["done"]),13),
    "weeksComplete": sum(1 for w in weeks if w["done"] and w["actual"]>0),
    "daysToRace":    (RACE_DATE-TODAY).days,
    "totalSwimTSS":  round(sum(a["tss"] for a in activities if a["disc"]=="swim")),
    "totalBikeTSS":  round(sum(a["tss"] for a in activities if a["disc"]=="bike")),
    "totalRunTSS":   round(sum(a["tss"] for a in activities if a["disc"]=="run")),
    "totalBikeSessions": len([a for a in activities if a["disc"]=="bike"]),
    "totalSwimSessions": len([a for a in activities if a["disc"]=="swim"]),
    "totalRunSessions":  len([a for a in activities if a["disc"]=="run"]),
}

with open("data/summary.json","w") as f:
    json.dump({"overall":overall,"weeks":weeks}, f, indent=2)
print(f"Done: {len(activities)} activities, {len(sleep_data)} sleep nights")
print(f"TSS — Swim: {overall['totalSwimTSS']}, Bike: {overall['totalBikeTSS']}, Run: {overall['totalRunTSS']}")
