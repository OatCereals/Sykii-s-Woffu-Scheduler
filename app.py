#!/usr/bin/env python3
"""
Sykii's Woffu Scheduler (web/headless) - clocks in/out from a per-day shift calendar.

Built for a Linux VPS (Debian 12). The UI is a web app; the scheduler runs in a thread.
For safety it listens ONLY on 127.0.0.1: access it through an SSH tunnel (see README).

    pip install flask requests holidays
    python3 app.py
"""

import os
import json
import time
import hashlib
import threading
from datetime import date, datetime, timedelta

import requests
from flask import Flask, request, jsonify, render_template

try:
    import holidays as holidays_lib
except ImportError:
    holidays_lib = None

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
os.makedirs(DATA, exist_ok=True)

SCHEDULE_FILE = os.path.join(DATA, "schedule.json")
STATE_FILE    = os.path.join(DATA, "state.json")
SETTINGS_FILE = os.path.join(DATA, "settings.json")
CACHE_FILE    = os.path.join(DATA, "cache.json")
SECRETS_FILE  = os.path.join(DATA, "secrets.json")
LOG_FILE      = os.path.join(DATA, "woffu.log")

DEFAULT_SETTINGS = {
    "country": "ES", "subdivision": "MD", "poll_seconds": 30,
    "jitter_minutes": 4,
    "salt": "",
    "presets": [
        {"name": "Morning S", "in": "06:50", "out": "13:50"},
        {"name": "Morning",   "in": "07:50", "out": "14:50"},
        {"name": "Morning L", "in": "07:50", "out": "15:50"},
        {"name": "Afternoon S",  "in": "16:50", "out": "22:50"},
        {"name": "Afternoon",    "in": "14:50", "out": "22:50"},
        {"name": "Afternoon L",  "in": "13:50", "out": "22:50"},
        {"name": "Night",    "in": "22:50", "out": "06:50"},
        {"name": "Night L",  "in": "21:50", "out": "06:50"},
    ],
    "descanso": {
        "enabled": True,
        "skip_on_nonworking": True,  # Sat/Sun/holiday: do not send a rest request
                                     # (the company already marks those days as rest)
        "window_start": "10:00",    # request is sent at a random time
        "window_end": "18:00",      # (deterministic per day) inside this window
        "agreementEvent": {         # captured from Woffu: company reason "Descanso"
            "agreementEventId": 2886718,
            "description": None,
            "documentRequired": False,
            "fractionDay": None,
            "isDocumentRequired": False,
            "isFractionDay": False,
            "isPresence": False,
            "isSelfAccepting": False,
            "isSlotRequired": False,
            "isVacation": False,
            "name": "Descanso",
            "repositoryAgreementEventId": None,
            "selfAccepting": False,
            "slotRequired": False,
            "useDays": True,
            "userStats": {
                "allocatedFormatted": None, "availableFormatted": None,
                "isFixed": False, "isNull": True, "isRepository": False,
                "nextYearUsedFormatted": None, "usedFormatted": None,
            },
            "isRepository": False,
        },
    },
    "correccion": {
        "enabled": True,
        "delay_min": 10,         # correction is sent between delay_min and delay_max minutes
        "delay_max": 20,         # after clock-out (deterministic random per day)
        "out_source": "real",    # "real" = time actually punched (Woffu may have capped it at 7h15)
                                 # "schedule" = that day's "out" from your calendar
    },
}

_lock = threading.Lock()
app = Flask(__name__)


# ----------------------------- storage -----------------------------

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default
    return default


def save_json(path, data):
    with _lock:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


def get_settings():
    s = dict(DEFAULT_SETTINGS)
    s.update(load_json(SETTINGS_FILE, {}))
    if not s.get("salt"):
        s["salt"] = hashlib.sha256(os.urandom(16)).hexdigest()[:16]
        save_json(SETTINGS_FILE, s)
    return s


def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ----------------------------- credentials -----------------------------

def get_credentials():
    u = os.environ.get("WOFFU_USERNAME")
    p = os.environ.get("WOFFU_PASSWORD")
    if u and p:
        return u, p
    sec = load_json(SECRETS_FILE, {})
    return sec.get("username"), sec.get("password")


def credentials_configured():
    u, p = get_credentials()
    return bool(u and p)


# ----------------------------- Woffu client -----------------------------

class WoffuError(Exception):
    pass


class WoffuAlreadyDone(Exception):
    """Woffu says the action was already done (duplicate / status changed)."""
    pass

_token_cache = {"token": None, "exp": 0}


def get_manual_token():
    """Token set by hand (from the browser). Takes priority over the password grant."""
    t = os.environ.get("WOFFU_TOKEN")
    if t:
        return t.strip()
    sec = load_json(SECRETS_FILE, {})
    t = (sec.get("token") or "").strip()
    return t or None


def get_token():
    manual = get_manual_token()
    if manual:
        return manual                       # use the browser token as-is
    now = time.time()
    if _token_cache["token"] and now < _token_cache["exp"]:
        return _token_cache["token"]
    u, p = get_credentials()
    if not (u and p):
        raise WoffuError("No credentials or token configured.")
    r = requests.post(
        "https://app.woffu.com/token",
        data={"grant_type": "password", "username": u, "password": p},
        headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
    if r.status_code != 200:
        raise WoffuError(f"Auth failed ({r.status_code}). Check username/password.")
    token = r.json().get("access_token")
    if not token:
        raise WoffuError("Response has no access_token.")
    _token_cache.update(token=token, exp=now + 80 * 24 * 3600)
    return token


def woffu_cookies():
    """The browser also sends the token as a cookie; we do the same."""
    return {"woffu.token": get_token()}


def auth_headers():
    return {"Authorization": "Bearer " + get_token(),
            "Accept": "application/json",
            "Content-Type": "application/json;charset=utf-8"}


def get_ids():
    c = load_json(CACHE_FILE, {})
    if all(k in c for k in ("domain", "user_id", "company_id")):
        return c["domain"], c["user_id"], c["company_id"]
    h = auth_headers()
    users = requests.get("https://app.woffu.com/api/users", headers=h, timeout=30).json()
    company = requests.get(f"https://app.woffu.com/api/companies/{users['CompanyId']}",
                           headers=h, timeout=30).json()
    ids = {"domain": company["Domain"], "user_id": users["UserId"], "company_id": users["CompanyId"]}
    save_json(CACHE_FILE, ids)
    return ids["domain"], ids["user_id"], ids["company_id"]


def send_sign():
    domain, _, _ = get_ids()
    now = datetime.now().astimezone()
    tz_minutes = -int(now.utcoffset().total_seconds() // 60)
    # Payload IDENTICAL to Woffu's web punch. The server sets the time.
    # No GPS: latitude/longitude omitted (same as denying location on the phone).
    # Do not send StartDate/EndDate (that caused odd auto-closes).
    payload = {
        "agreementEventId": None,
        "requestId": None,
        "deviceId": "WebApp",
        "timezoneOffset": tz_minutes,
    }
    headers = auth_headers()
    headers.update({
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": f"https://{domain}",
        "Referer": f"https://{domain}/v2/personal/dashboard/user",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
        "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    })
    r = requests.post(f"https://{domain}/api/svc/signs/signs",
                      json=payload, headers=headers, cookies=woffu_cookies(), timeout=30)
    log(f"SIGN-RESP status={r.status_code} body={r.text[:800]}")
    if not r.ok:
        raise WoffuError(f"Punch rejected ({r.status_code}): {r.text[:200]}")
    return True


def send_rest(iso):
    """Create a 'Descanso' absence request for day iso (start=end=iso)."""
    domain, user_id, company_id = get_ids()
    ev = get_settings()["descanso"]["agreementEvent"]
    payload = {
        "agreementEventId": ev["agreementEventId"],
        "isVacation": False,
        "numberHoursRequested": 0,
        "quickDescription": "",
        "responsibleUserId": 0,
        "userId": user_id,
        "files": [],
        "companyId": company_id,
        "accepted": False,
        "documents": [],
        "startTime": None,
        "endTime": None,
        "numberDaysRequested": 1,
        "endDate": iso,
        "startDate": iso,
        "agreementEvent": ev,
    }
    r = requests.post(f"https://{domain}/api/requests",
                      json=payload, headers=auth_headers(), cookies=woffu_cookies(), timeout=30)
    if not r.ok:
        body = r.text[:200]
        # Woffu rejects duplicates / status changes with this message:
        # treat as "already done", not as a failure to retry.
        if r.status_code == 400 and "_AppStatusChangedError" in body:
            raise WoffuAlreadyDone(f"Rest request already exists or status changed: {body}")
        raise WoffuError(f"Rest request rejected ({r.status_code}): {body}")
    return True


# ----------------------------- night-shift correction -----------------------------
# Woffu, if misconfigured, auto-closes a night shift at a 7h15 cap
# (valueTime = in + 7:15) even if clock-out was later. The correction
# reads the workday, sets the real out time, and rewrites totalMin.

_WEB_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


def _hhmmss_to_sec(t):
    p = [int(x) for x in str(t).split(":")]
    while len(p) < 3:
        p.append(0)
    return p[0] * 3600 + p[1] * 60 + p[2]


def _month_bounds(iso):
    """(first day, last day) of the month of `iso`, in ISO format."""
    first = date.fromisoformat(iso).replace(day=1)
    nxt = (first.replace(year=first.year + 1, month=1) if first.month == 12
           else first.replace(month=first.month + 1))
    return first.isoformat(), (nxt - timedelta(days=1)).isoformat()


def _woffu_get(url):
    domain, _, _ = get_ids()
    headers = auth_headers()
    headers.update(_WEB_HEADERS)
    headers["Referer"] = f"https://{domain}/v2/personal/diary/user"
    r = requests.get(url, headers=headers, cookies=woffu_cookies(), timeout=30)
    if not r.ok:
        raise WoffuError(f"GET {r.status_code}: {r.text[:200]}")
    try:
        return r.json()
    except ValueError:
        raise WoffuError(f"Non-JSON response: {r.text[:200]}")


def fetch_diary_summary_id(iso):
    """Step 1: in the month table (presence), find the diarySummaryId for day iso."""
    domain, user_id, _ = get_ids()
    frm, to = _month_bounds(iso)
    url = (f"https://{domain}/api/svc/core/diariesquery/users/{user_id}"
           f"/diaries/summary/presence?userId={user_id}"
           f"&fromDate={frm}&toDate={to}&pageSize=31"
           f"&includeHourTypes=true&includeNotHourTypes=true&includeDifference=true")
    data = _woffu_get(url)
    for d in data.get("diaries", []):
        if d.get("date", "").startswith(iso):
            sid = d.get("diarySummaryId")
            log(f"PRESENCE {iso} -> diarySummaryId={sid} (in={d.get('in')} out={d.get('out')})")
            return sid
    raise WoffuError(f"Could not find day {iso} in the month table.")


def fetch_workday(iso):
    """Step 2: with the diarySummaryId, read the day's detail (slots with signId).
    Returns {'date','comments','slots':[{'in','out','motive'}]}."""
    domain, _, _ = get_ids()
    sid = fetch_diary_summary_id(iso)
    url = (f"https://{domain}/api/svc/core/diariesquery/diarysummaries/{sid}"
           f"/workday/slots/self")
    wd = _woffu_get(url)
    base = wd.get("diaryBaseWorkDay") or {}
    slots = wd.get("slots") or []
    log(f"WORKDAY {iso} sid={sid} slots={len(slots)}")
    return {
        "date": base.get("date", iso)[:10],
        "comments": base.get("comments") or "",
        "slots": slots,
    }


def _night_slot(workday):
    """Slot for a shift that crosses midnight (real out < real in). If there is
    no clear crossing, the first slot with in and out."""
    slots = workday.get("slots") or []
    for sl in slots:
        i, o = sl.get("in") or {}, sl.get("out") or {}
        ti, to = i.get("shortTime"), o.get("shortTime")
        if ti and to and _hhmmss_to_sec(to) < _hhmmss_to_sec(ti):
            return sl
    for sl in slots:
        if sl.get("in") and sl.get("out"):
            return sl
    return None


def _real_out_hhmmss(out):
    """Local time actually punched for clock-out. `shortTime` is the raw punch
    (Woffu only trims the 'true'/'value' fields), so it is the reliable source."""
    return out.get("shortTime") or out.get("time")


def _target_out_hhmmss(iso, slot):
    """Out time to set. 'real' = what was actually punched (what Woffu
    trimmed); 'schedule' = that day's 'out' from your calendar."""
    cfg = get_settings().get("correccion", {})
    if cfg.get("out_source", "real") == "schedule":
        ot = (load_json(SCHEDULE_FILE, {}).get(iso, {}) or {}).get("out")
        if ot:
            return ot if ot.count(":") == 2 else ot + ":00"
    return _real_out_hhmmss(slot.get("out") or {})


def send_correction(iso, workday=None):
    """Read the workday for `iso`, fix the out time Woffu trimmed, and PUT.
    Raises WoffuAlreadyDone if there is nothing to correct or it was already OK."""
    domain, user_id, _ = get_ids()
    if workday is None:
        workday = fetch_workday(iso)
    slot = _night_slot(workday)
    if not slot:
        raise WoffuAlreadyDone(f"{iso}: no in+out shift to correct.")

    inn, out = slot.get("in") or {}, slot.get("out") or {}
    target = _target_out_hhmmss(iso, slot)
    if not target:
        raise WoffuError(f"{iso}: could not determine the corrected out time.")

    # The web rounds the corrected out time to the minute (e.g. 06:46:22 -> 06:46:00).
    hh, mm = target.split(":")[0], target.split(":")[1]
    target = f"{hh}:{mm}:00"

    # Current effective out (what Woffu counts). If it already covers the target, nothing to do.
    effective = out.get("time") or out.get("shortValueTime") or "00:00:00"
    if _hhmmss_to_sec(effective) >= _hhmmss_to_sec(target) - 60:
        raise WoffuAlreadyDone(
            f"{iso}: out time already OK (effective {effective} >= target {target}).")

    # Real totalMin: from punched in to target out, crossing midnight.
    in_sec = _hhmmss_to_sec(inn.get("shortTime") or inn.get("time") or "0")
    out_sec = _hhmmss_to_sec(target)
    if out_sec <= in_sec:
        out_sec += 24 * 3600
    total_min = (out_sec - in_sec) // 60

    # Same mutation as a manual correction in the web:
    #  - out.time = corrected time (to the minute)
    #  - signType=3 / signStatus=1  -> turns the auto-close (5/0) into
    #    "punch corrected by the user"; WITHOUT this Woffu returns 200 but does NOT compute it.
    #  - slot.totalMin recalculated. Woffu derives the rest (valueTime, trueDate...).
    out["time"] = target
    out["signType"] = 3
    out["signStatus"] = 1
    slot["totalMin"] = total_min
    slot.setdefault("id", f"{int(time.time() * 1000)}-0")

    payload = {
        "date": workday.get("date", iso),
        "comments": workday.get("comments", "") or "",
        "userId": user_id,
        "slots": workday.get("slots"),
    }
    url = (f"https://{domain}/api/svc/core/users/{user_id}"
           f"/diarysummaries/workday/slots/self")
    headers = auth_headers()
    headers.update(_WEB_HEADERS)
    headers["Origin"] = f"https://{domain}"
    headers["Referer"] = f"https://{domain}/v2/personal/diary/user"
    r = requests.put(url, json=payload, headers=headers, cookies=woffu_cookies(), timeout=30)
    log(f"CORRECTION-PUT {iso} out={target} total={total_min} "
        f"status={r.status_code} body={r.text[:400]}")
    if not r.ok:
        body = r.text[:200]
        if r.status_code == 400 and "_AppStatusChangedError" in body:
            raise WoffuAlreadyDone(f"{iso}: status already changed: {body}")
        raise WoffuError(f"Correction rejected ({r.status_code}): {body}")
    return {"out": target, "totalMin": total_min}


# ----------------------------- idempotency -----------------------------

MAX_RETRIES = 4     # max attempts on network errors before giving up that day
_fail_counts = {}   # {(iso, action): n}  (in memory, resets when the service restarts)


def _bump_fail(iso, action):
    k = (iso, action)
    _fail_counts[k] = _fail_counts.get(k, 0) + 1
    return _fail_counts[k]


def is_done(iso, action):
    return action in load_json(STATE_FILE, {}).get(iso, [])


def mark_done(iso, action):
    state = load_json(STATE_FILE, {})
    state.setdefault(iso, [])
    if action not in state[iso]:
        state[iso].append(action)
    state = {d: v for d, v in state.items()
             if (date.today() - date.fromisoformat(d)).days <= 90}
    save_json(STATE_FILE, state)


# ----------------------------- holidays (informational) -----------------------------

def holidays_for_month(year, month):
    if holidays_lib is None:
        return {}
    s = get_settings()
    try:
        h = holidays_lib.country_holidays(s["country"], subdiv=s.get("subdivision") or None,
                                          years=[year])
    except Exception:
        return {}
    return {d.isoformat(): n for d, n in h.items() if d.month == month}


def is_nonworking_day(iso):
    """True if `iso` is Saturday, Sunday, or a holiday (per country/subdivision).
    On those days the company already marks rest, so we do not send a request."""
    d = date.fromisoformat(iso)
    if d.weekday() >= 5:            # 5=Saturday, 6=Sunday
        return True
    if holidays_lib is None:
        return False
    s = get_settings()
    try:
        h = holidays_lib.country_holidays(s["country"], subdiv=s.get("subdivision") or None,
                                          years=[d.year])
        return d in h
    except Exception:
        return False


# ----------------------------- scheduler -----------------------------

def hhmm_to_min(t):
    hh, mm = t.split(":")
    return int(hh) * 60 + int(mm)


def jitter_delta(iso, action):
    """Delay after the shift time: 0 seconds through jitter_minutes exactly
    (e.g. 4 -> 0:00 .. 4:00, so 2:13 is allowed, 4:13 is not).
    Deterministic per day and action so a restart does not pick a new time."""
    s = get_settings()
    jmax = int(s.get("jitter_minutes", 0))
    if jmax <= 0:
        return timedelta(0)
    salt = s.get("salt", "")
    h = hashlib.sha256(f'{s.get("salt","")}|{iso}|{action}'.encode()).hexdigest()
    return timedelta(seconds=int(h, 16) % (jmax * 60 + 1))


def due_at(day, hhmm, iso, action):
    hh, mm = hhmm.split(":")[:2]
    return datetime(day.year, day.month, day.day, int(hh), int(mm), 0) + jitter_delta(iso, action)


def correction_delay(iso):
    """Minutes (deterministic per day) to wait after clock-out before correcting,
    random within [delay_min, delay_max]. Accepts the old delay_minutes key."""
    c = get_settings().get("correccion", {})
    lo = int(c.get("delay_min", c.get("delay_minutes", 10)))
    hi = int(c.get("delay_max", c.get("delay_minutes", 20)))
    if hi < lo:
        lo, hi = hi, lo
    if hi == lo:
        return lo
    h = hashlib.sha256(f'{get_settings().get("salt","")}|{iso}|fix-delay'.encode()).hexdigest()
    return lo + int(h, 16) % (hi - lo + 1)


def rest_due_at(iso):
    """When to send the rest request: random minute in the window, plus 0..59 seconds."""
    s = get_settings()
    d = s.get("descanso", {})
    start = hhmm_to_min(d.get("window_start", "10:00"))
    end = hhmm_to_min(d.get("window_end", "18:00"))
    minute = start
    if end > start:
        h = hashlib.sha256(f'{s.get("salt","")}|{iso}|descanso-window'.encode()).hexdigest()
        minute = start + int(h, 16) % (end - start + 1)
    h2 = hashlib.sha256(f'{s.get("salt","")}|{iso}|descanso-window|sec'.encode()).hexdigest()
    day = date.fromisoformat(iso)
    return datetime(day.year, day.month, day.day, 0, 0, 0) + timedelta(
        minutes=minute, seconds=int(h2, 16) % 60)


def _fire(iso, action):
    try:
        send_sign()
        mark_done(iso, action)
        log(f"{iso} {action} OK")
    except (WoffuError, requests.RequestException) as e:
        n = _bump_fail(iso, action)
        log(f"{iso} {action} FAIL ({n}/{MAX_RETRIES}): {e}")
        if n >= MAX_RETRIES:
            mark_done(iso, action)
            log(f"{iso} {action}: retry limit reached, giving up.")


def _fire_rest(iso):
    try:
        send_rest(iso)
        mark_done(iso, "descanso")
        log(f"{iso} rest OK (request sent)")
    except WoffuAlreadyDone as e:
        # Already exists in Woffu: count it as done and do NOT retry.
        mark_done(iso, "descanso")
        log(f"{iso} rest ALREADY EXISTS, not retrying: {e}")
    except (WoffuError, requests.RequestException) as e:
        # Other error (network, etc.): retry a few times, not all day.
        n = _bump_fail(iso, "descanso")
        log(f"{iso} rest FAIL ({n}/{MAX_RETRIES}): {e}")
        if n >= MAX_RETRIES:
            mark_done(iso, "descanso")
            log(f"{iso} rest: retry limit reached, giving up.")


def _fire_correction(iso):
    try:
        res = send_correction(iso)
        mark_done(iso, "fix")
        log(f"{iso} correction OK -> out {res['out']} ({res['totalMin']} min)")
    except WoffuAlreadyDone as e:
        mark_done(iso, "fix")
        log(f"{iso} correction not needed/already done: {e}")
    except (WoffuError, requests.RequestException) as e:
        n = _bump_fail(iso, "fix")
        log(f"{iso} correction FAIL ({n}/{MAX_RETRIES}): {e}")
        if n >= MAX_RETRIES:
            mark_done(iso, "fix")
            log(f"{iso} correction: retry limit reached, giving up.")


def tick():
    """Run due actions. Return seconds to sleep until the next one (capped by poll)."""
    poll = float(get_settings().get("poll_seconds", 30))
    if not credentials_configured():
        return poll
    sched = load_json(SCHEDULE_FILE, {})
    now = datetime.now()
    today = now.date()
    iso = today.isoformat()
    yiso = (today - timedelta(days=1)).isoformat()
    now_min = now.hour * 60 + now.minute
    next_wait = poll

    def note(dt):
        nonlocal next_wait
        if dt is None:
            return False
        wait = (dt - now).total_seconds()
        if wait > 0:
            next_wait = min(next_wait, wait)
            return False
        return True

    s = sched.get(iso)
    if s and s.get("rest"):
        dcfg = get_settings().get("descanso", {})
        if dcfg.get("skip_on_nonworking", True) and is_nonworking_day(iso):
            if not is_done(iso, "descanso"):
                mark_done(iso, "descanso")
                log(f"{iso} rest SKIPPED (weekend/holiday, company already marks it)")
        elif dcfg.get("enabled", True) and not is_done(iso, "descanso"):
            tgt = rest_due_at(iso)
            if note(tgt):
                _fire_rest(iso)
        s = None

    if s:
        in_t, out_t = s.get("in"), s.get("out")
        in_min = hhmm_to_min(in_t) if in_t else None
        out_min = hhmm_to_min(out_t) if out_t else None
        same_day = in_min is not None and out_min is not None and out_min > in_min

        if in_t and not is_done(iso, "in"):
            tgt = due_at(today, in_t, iso, "in")
            if note(tgt) and (not same_day or now_min < out_min):
                _fire(iso, "in")

        if out_t and same_day and is_done(iso, "in") and not is_done(iso, "out"):
            tgt = due_at(today, out_t, iso, "out")
            if note(tgt):
                _fire(iso, "out")

    sy = sched.get(yiso)
    if sy:
        yout = sy.get("out")
        yin_min = hhmm_to_min(sy["in"]) if sy.get("in") else 0
        yout_min = hhmm_to_min(yout) if yout else None
        crosses = yout_min is not None and yout_min <= yin_min
        # Out is this morning (today's clock), stored on yesterday's shift.
        if crosses and is_done(yiso, "in") and not is_done(yiso, "out"):
            tgt = due_at(today, yout, yiso, "out")
            if note(tgt) and now_min < yin_min:
                _fire(yiso, "out")

        ccfg = get_settings().get("correccion", {})
        if (crosses and ccfg.get("enabled", True)
                and is_done(yiso, "out") and not is_done(yiso, "fix")):
            tgt = due_at(today, yout, yiso, "out") + timedelta(minutes=correction_delay(yiso))
            if note(tgt) and now_min < yin_min:
                _fire_correction(yiso)

    return max(0.2, next_wait)


def scheduler_loop():
    while True:
        try:
            wait = tick()
        except Exception as e:
            log(f"scheduler error: {e}")
            wait = 30
        time.sleep(wait)


# ----------------------------- routes -----------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/month/<int:year>/<int:month>")
def api_month(year, month):
    schedule = load_json(SCHEDULE_FILE, {})
    state = load_json(STATE_FILE, {})
    days = {d: v for d, v in schedule.items() if d.startswith(f"{year:04d}-{month:02d}-")}
    return jsonify({
        "days": days,
        "holidays": holidays_for_month(year, month),
        "state": {d: v for d, v in state.items() if d.startswith(f"{year:04d}-{month:02d}-")},
        "today": date.today().isoformat(),
    })


@app.route("/api/day", methods=["POST"])
def api_set_day():
    body = request.get_json(force=True)
    d = body.get("date")
    if not d:
        return jsonify({"error": "Missing date."}), 400
    schedule = load_json(SCHEDULE_FILE, {})
    if body.get("rest"):
        schedule[d] = {"rest": True}
    else:
        in_t, out_t = body.get("in"), body.get("out")
        if not in_t or not out_t:
            return jsonify({"error": "Missing in or out."}), 400
        schedule[d] = {"in": in_t, "out": out_t}
    save_json(SCHEDULE_FILE, schedule)
    return jsonify({"ok": True, "day": schedule[d]})


@app.route("/api/day/<d>", methods=["DELETE"])
def api_del_day(d):
    schedule = load_json(SCHEDULE_FILE, {})
    schedule.pop(d, None)
    save_json(SCHEDULE_FILE, schedule)
    state = load_json(STATE_FILE, {})
    state.pop(d, None)
    save_json(STATE_FILE, state)
    return jsonify({"ok": True})


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "POST":
        s = get_settings()
        s.update(request.get_json(force=True))
        save_json(SETTINGS_FILE, s)
        return jsonify({"ok": True, "settings": s})
    return jsonify(get_settings())


@app.route("/api/status")
def api_status():
    iso = date.today().isoformat()
    sched = load_json(SCHEDULE_FILE, {}).get(iso)
    state = load_json(STATE_FILE, {}).get(iso, [])
    return jsonify({
        "credentials": credentials_configured(),
        "today": iso,
        "today_shift": sched,
        "today_done": state,
        "now": datetime.now().strftime("%H:%M"),
    })


@app.route("/api/credentials", methods=["POST"])
def api_credentials():
    body = request.get_json(force=True)
    u, p = body.get("username"), body.get("password")
    if not u or not p:
        return jsonify({"error": "Missing username/password."}), 400
    save_json(SECRETS_FILE, {"username": u, "password": p})
    _token_cache["token"] = None
    return jsonify({"ok": True})


@app.route("/api/test-auth", methods=["POST"])
def api_test_auth():
    try:
        get_token()
        domain, user_id, _ = get_ids()
        return jsonify({"ok": True, "domain": domain, "user_id": user_id})
    except (WoffuError, requests.RequestException) as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/sign-now", methods=["POST"])
def api_sign_now():
    try:
        send_sign()
        return jsonify({"ok": True})
    except (WoffuError, requests.RequestException) as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/workday/<iso>")
def api_workday(iso):
    """Debug: return the workday as Woffu sees it (to confirm the GET format).
    e.g. /api/workday/2026-07-13"""
    try:
        return jsonify(fetch_workday(iso))
    except (WoffuError, requests.RequestException) as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/fix-now", methods=["POST"])
def api_fix_now():
    """Correct the night shift for a date (default: yesterday).
    Optional body: {"date": "2026-07-13"} = IN date of the shift."""
    body = request.get_json(silent=True) or {}
    iso = body.get("date") or (date.today() - timedelta(days=1)).isoformat()
    try:
        res = send_correction(iso)
        mark_done(iso, "fix")
        return jsonify({"ok": True, "date": iso, **res})
    except WoffuAlreadyDone as e:
        mark_done(iso, "fix")
        return jsonify({"ok": True, "already": True, "date": iso, "msg": str(e)})
    except (WoffuError, requests.RequestException) as e:
        return jsonify({"ok": False, "date": iso, "error": str(e)}), 400


def start_scheduler():
    threading.Thread(target=scheduler_loop, daemon=True).start()


if __name__ == "__main__":
    start_scheduler()
    # Bind all interfaces so you can open http://raspberry-ip:5000 from another PC.
    # There is no login: anyone on your LAN/Wi-Fi can use the UI. Do not port-forward
    # 5000 on the router. Set WOFFU_HOST=127.0.0.1 to listen on localhost only.
    host = os.environ.get("WOFFU_HOST", "0.0.0.0")
    port = int(os.environ.get("WOFFU_PORT", "5000"))
    print(f"Sykii's Woffu Scheduler at http://{host}:{port}")
    app.run(host=host, port=port, debug=False, use_reloader=False)
