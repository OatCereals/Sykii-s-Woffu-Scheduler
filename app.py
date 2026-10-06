#!/usr/bin/env python3
"""
kinkyscheduler — multi-user Woffu shift calendar + background punches.

    pip install flask requests holidays
    python3 app.py
"""

import os
import re
import json
import time
import hashlib
import secrets
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from functools import wraps

import requests
from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash

try:
    import holidays as holidays_lib
except ImportError:
    holidays_lib = None

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
os.makedirs(DATA, exist_ok=True)
ACCOUNTS_FILE = os.path.join(DATA, "accounts.json")


def load_env_file():
    """Load KEY=VAL from .env into os.environ (does not override existing)."""
    for candidate in (
        os.path.join(BASE, ".env"),
        os.path.join(BASE, "docker", ".env"),
    ):
        if not os.path.isfile(candidate):
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    key = key.strip()
                    val = val.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = val
        except OSError:
            pass


load_env_file()

DEFAULT_SETTINGS = {
    "country": "ES", "subdivision": "MD", "poll_seconds": 30,
    "jitter_before_minutes": 2,
    "jitter_after_minutes": 1,
    "salt": "",
    "presets": [
        {"name": "Morning S", "in": "06:50", "out": "13:50"},
        {"name": "Morning",   "in": "06:50", "out": "14:50"},
        {"name": "Morning L", "in": "06:50", "out": "15:50"},
        {"name": "Afternoon S",  "in": "16:50", "out": "22:50"},
        {"name": "Afternoon",    "in": "14:50", "out": "22:50"},
        {"name": "Afternoon L",  "in": "13:50", "out": "22:50"},
        {"name": "Night",    "in": "22:50", "out": "06:50"},
        {"name": "Night L",  "in": "21:50", "out": "06:50"},
        {"name": "Office",    "in": "09:00", "out": "17:00"},
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
        "delay_min": 30,         # seconds after out (deterministic random per day/user)
        "delay_max": 180,        # 30s .. 3 min before read/correct
        "out_source": "auto",    # "auto" = later of calendar out vs punched out (fixes 7h15 cap)
                                 # "schedule" = that day's "out" from your calendar
                                 # "real" = time actually punched
    },
}

_lock = threading.Lock()
_ctx = threading.local()
_token_caches = {}   # username -> {token, exp}
_fail_counts = {}    # (username, iso, action) -> n

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Set SESSION_COOKIE_SECURE=1 behind HTTPS (Caddy). Cookies won't send on plain HTTP.
_secure = (os.environ.get("SESSION_COOKIE_SECURE") or "").strip().lower()
app.config["SESSION_COOKIE_SECURE"] = _secure in ("1", "true", "yes", "on")
# Trust X-Forwarded-* from the reverse proxy
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


# ----------------------------- env helpers -----------------------------

def register_code():
    return (os.environ.get("REGISTER_CODE") or "aleksejsisblin").strip()


def safe_username(name):
    name = (name or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{3,32}", name):
        return None
    return name


def user_data_dir(username=None):
    u = username or getattr(_ctx, "username", None)
    if not u:
        raise RuntimeError("no user context")
    d = os.path.join(DATA, "users", u)
    os.makedirs(d, exist_ok=True)
    return d


def upath(filename, username=None):
    return os.path.join(user_data_dir(username), filename)


@contextmanager
def user_scope(username):
    prev = getattr(_ctx, "username", None)
    _ctx.username = username
    try:
        yield
    finally:
        _ctx.username = prev


def current_app_user():
    return session.get("user")


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
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with _lock:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


def load_accounts():
    return load_json(ACCOUNTS_FILE, {})


def save_accounts(accounts):
    save_json(ACCOUNTS_FILE, accounts)


def list_usernames():
    return sorted(load_accounts().keys())


def get_settings():
    s = dict(DEFAULT_SETTINGS)
    s.update(load_json(upath("settings.json"), {}))
    if not s.get("salt"):
        s["salt"] = hashlib.sha256(os.urandom(16)).hexdigest()[:16]
        try:
            save_json(upath("settings.json"), s)
        except OSError as e:
            log(f"could not write settings: {e}")
    return s


def log(msg):
    user = getattr(_ctx, "username", None)
    prefix = f"[{user}] " if user else ""
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {prefix}{msg}"
    print(line, flush=True)
    try:
        log_path = upath("woffu.log") if user else os.path.join(DATA, "woffu.log")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ----------------------------- credentials (per app user → Woffu) -----------------------------

def get_credentials():
    sec = load_json(upath("secrets.json"), {})
    return sec.get("username"), sec.get("password")


def credentials_configured():
    u, p = get_credentials()
    return bool(u and p)


def credentials_verified():
    """True/False if Save-and-test was run; None if never verified."""
    sec = load_json(upath("secrets.json"), {})
    if "verified" not in sec:
        return None
    return bool(sec.get("verified"))


def set_credentials_verified(ok):
    sec = load_json(upath("secrets.json"), {})
    sec["verified"] = bool(ok)
    save_json(upath("secrets.json"), sec)


# ----------------------------- Woffu client -----------------------------

class WoffuError(Exception):
    pass


class WoffuAlreadyDone(Exception):
    """Woffu says the action was already done (duplicate / status changed)."""
    pass


def get_manual_token():
    """Token set by hand (from the browser). Takes priority over the password grant."""
    sec = load_json(upath("secrets.json"), {})
    t = (sec.get("token") or "").strip()
    return t or None


def get_token():
    manual = get_manual_token()
    if manual:
        return manual
    uname = getattr(_ctx, "username", None) or "_"
    cache = _token_caches.setdefault(uname, {"token": None, "exp": 0})
    now = time.time()
    if cache["token"] and now < cache["exp"]:
        return cache["token"]
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
    cache.update(token=token, exp=now + 80 * 24 * 3600)
    return token


def woffu_cookies():
    """The browser also sends the token as a cookie; we do the same."""
    return {"woffu.token": get_token()}


def auth_headers():
    return {"Authorization": "Bearer " + get_token(),
            "Accept": "application/json",
            "Content-Type": "application/json;charset=utf-8"}


def get_ids():
    c = load_json(upath("cache.json"), {})
    if all(k in c for k in ("domain", "user_id", "company_id")):
        return c["domain"], c["user_id"], c["company_id"]
    h = auth_headers()
    users = requests.get("https://app.woffu.com/api/users", headers=h, timeout=30).json()
    company = requests.get(f"https://app.woffu.com/api/companies/{users['CompanyId']}",
                           headers=h, timeout=30).json()
    ids = {"domain": company["Domain"], "user_id": users["UserId"], "company_id": users["CompanyId"]}
    save_json(upath("cache.json"), ids)
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


# ----------------------------- out-time correction -----------------------------
# Woffu, if misconfigured, auto-closes a shift at a 7h15 cap
# (valueTime = in + 7:15) even if clock-out was later (e.g. 14:50→22:50
# becomes 22:05). The correction reads the workday, sets the calendar /
# real out time, and rewrites totalMin.

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


def _side_time(side):
    side = side or {}
    return side.get("shortTime") or side.get("time")


def _sign_id(side):
    side = side or {}
    sid = side.get("signId", side.get("SignId", 0))
    try:
        return int(sid or 0)
    except (TypeError, ValueError):
        return 0


def _sign_type(side):
    side = side or {}
    st = side.get("signType", side.get("SignType"))
    try:
        return int(st) if st is not None else None
    except (TypeError, ValueError):
        return None


def _is_real_sign(side):
    """True if this in/out is an actual punch/auto-close, not a schedule proposal.

    Office workdays often return 2 slots with times filled in and signId=0 —
    those are the UI's theoretical schedule, NOT a real clock-out. Treating
    them as closed made us skip the evening punch."""
    if not side or not _side_time(side):
        return False
    if _sign_id(side) > 0:
        return True
    st = _sign_type(side)
    # signType 1 = proposal; 3 = manual/corrected; 5 = auto-close (seen in app)
    return st is not None and st != 1


def _slot_in_time(slot):
    return _side_time((slot or {}).get("in"))


def _slot_has_real_out(slot):
    """True only if Woffu has an actual clock-out sign (not a proposal out)."""
    if not slot:
        return False
    return _is_real_sign(slot.get("out"))


def _slot_has_real_in(slot):
    if not slot:
        return False
    return _is_real_sign(slot.get("in"))


def _slot_brief(slot):
    """Short log line for a slot (times + whether signs look real)."""
    inn, out = (slot or {}).get("in") or {}, (slot or {}).get("out") or {}
    ti = _side_time(inn) or "-"
    to = _side_time(out) or "-"
    ri = "real" if _is_real_sign(inn) else f"prop/sid{_sign_id(inn)}"
    ro = "real" if _is_real_sign(out) else f"prop/sid{_sign_id(out)}"
    return f"{ti}({ri})->{to}({ro})"


def _open_slots(workday):
    """Segments with a real clock-in but no real clock-out (still open)."""
    return [
        sl for sl in (workday.get("slots") or [])
        if _slot_has_real_in(sl) and not _slot_has_real_out(sl)
    ]


def _closed_slots(workday):
    return [
        sl for sl in (workday.get("slots") or [])
        if _slot_has_real_in(sl) and _slot_has_real_out(sl)
    ]


def _night_slot(workday):
    """Slot for a shift that crosses midnight (real out < real in). Otherwise the
    last closed slot (end-of-day), not the first — Office days often have a lunch
    slot closed while the afternoon segment is still open."""
    for sl in _closed_slots(workday):
        ti, to = _slot_in_time(sl), _real_out_hhmmss((sl.get("out") or {}))
        if ti and to and _hhmmss_to_sec(to) < _hhmmss_to_sec(ti):
            return sl
    closed = _closed_slots(workday)
    return closed[-1] if closed else None


def _real_out_hhmmss(out):
    """Local time actually punched for clock-out. `shortTime` is the raw punch
    (Woffu only trims the 'true'/'value' fields), so it is the reliable source."""
    return out.get("shortTime") or out.get("time")


def _norm_hhmmss(t):
    if not t:
        return None
    return t if str(t).count(":") >= 2 else str(t) + ":00"


def _target_out_hhmmss(iso, slot):
    """Out time to set. Prefer the later of calendar out vs punched out so a
    7h15 auto-close (e.g. 22:05) does not beat a 22:50 shift."""
    real = _norm_hhmmss(_real_out_hhmmss(slot.get("out") or {}))
    ot = (load_json(upath("schedule.json"), {}).get(iso, {}) or {}).get("out")
    sched = _norm_hhmmss(ot)
    mode = get_settings().get("correccion", {}).get("out_source", "auto")
    if mode == "schedule" and sched:
        return sched
    if mode == "real" and real:
        if sched and _hhmmss_to_sec(sched) > _hhmmss_to_sec(real) + 60:
            return sched
        return real
    if sched and real:
        return sched if _hhmmss_to_sec(sched) > _hhmmss_to_sec(real) else real
    return sched or real


def shift_already_has_out(iso):
    """True only if every started segment is closed (no open slot left).

    Office / split days often have 2 slots (morning+lunch out, afternoon still
    open). Treating 'any closed slot' as done skipped the real clock-out."""
    try:
        wd = fetch_workday(iso)
    except (WoffuError, requests.RequestException) as e:
        log(f"{iso} could not read workday before out: {e}")
        return False
    slots = wd.get("slots") or []
    briefs = ", ".join(_slot_brief(sl) for sl in slots) or "(none)"
    open_sl = _open_slots(wd)
    if open_sl:
        log(f"{iso} still open — slots=[{briefs}]; will punch out")
        return False
    closed = _closed_slots(wd)
    if closed:
        log(f"{iso} all segments closed — slots=[{briefs}]; skip punch, will correct")
        return True
    log(f"{iso} no closed segment yet — slots=[{briefs}]; will punch out")
    return False


def send_correction(iso, workday=None):
    """Read the workday for `iso`, fix the out time Woffu trimmed, and PUT.
    Raises WoffuAlreadyDone if there is nothing to correct or it was already OK."""
    domain, user_id, _ = get_ids()
    if workday is None:
        workday = fetch_workday(iso)
    # Never correct while a segment is still open — punch first.
    if _open_slots(workday):
        raise WoffuAlreadyDone(f"{iso}: open slot still; skip correction.")
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

    # Current effective out (what Woffu counts). Prefer shortTime too — presence
    # can already show a good close while trimmed fields look early.
    effective = (
        out.get("time") or out.get("shortValueTime")
        or out.get("shortTime") or "00:00:00"
    )
    # Don't undo punch jitter: tolerate at least the configured "out early"
    # window (plus 30s). Old hard-coded 60s rewrote e.g. 16:58 → 17:00 when
    # jitter_out_before was 2. Still corrects real 7h15 auto-closes (hours early).
    s = get_settings()
    g_before = s.get("jitter_before_minutes", s.get("jitter_minutes", 2))
    out_before = s.get("jitter_out_before_minutes", g_before)
    try:
        out_before = max(0, int(out_before if out_before is not None else 0))
    except (TypeError, ValueError):
        out_before = 0
    tol_sec = max(60, out_before * 60 + 30)
    if _hhmmss_to_sec(effective) >= _hhmmss_to_sec(target) - tol_sec:
        raise WoffuAlreadyDone(
            f"{iso}: out time already OK (effective {effective} >= target {target}, "
            f"tol={tol_sec}s).")

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


def _bump_fail(iso, action):
    k = (getattr(_ctx, "username", None), iso, action)
    _fail_counts[k] = _fail_counts.get(k, 0) + 1
    return _fail_counts[k]


def is_done(iso, action):
    return action in load_json(upath("state.json"), {}).get(iso, [])


def mark_done(iso, action):
    state = load_json(upath("state.json"), {})
    state.setdefault(iso, [])
    if action not in state[iso]:
        state[iso].append(action)
    state = {d: v for d, v in state.items()
             if (date.today() - date.fromisoformat(d)).days <= 90}
    save_json(upath("state.json"), state)


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


def is_weekend(iso):
    """Saturday or Sunday. Woffu already treats these as rest unless a shift is set."""
    return date.fromisoformat(iso).weekday() >= 5  # 5=Saturday, 6=Sunday


def is_holiday(iso):
    """Official public holiday for settings country/subdivision (default ES/Madrid)."""
    d = date.fromisoformat(iso)
    if holidays_lib is None:
        return False
    s = get_settings()
    try:
        h = holidays_lib.country_holidays(s["country"], subdiv=s.get("subdivision") or None,
                                          years=[d.year])
        return d in h
    except Exception:
        return False


def woffu_already_rest(iso):
    """Weekends and official festivos: Woffu already counts them as rest unless a shift is set."""
    return is_weekend(iso) or is_holiday(iso)


def is_nonworking_day(iso):
    """True if `iso` is Saturday, Sunday, or a holiday (per country/subdivision).
    On those days the company already marks rest, so we do not send a request."""
    return woffu_already_rest(iso)


# ----------------------------- scheduler -----------------------------

def hhmm_to_min(t):
    hh, mm = t.split(":")
    return int(hh) * 60 + int(mm)


def jitter_offset(iso, action):
    """Signed offset from the calendar time (deterministic per user/day/action).
    Negative = before, positive = after. Uniform over
    [-before_min, +after_min] in whole seconds.

    Per-action keys (preferred):
      jitter_in_before_minutes / jitter_in_after_minutes
      jitter_out_before_minutes / jitter_out_after_minutes
    Fall back to global jitter_before_minutes / jitter_after_minutes
    (legacy jitter_minutes = before-only if before unset)."""
    s = get_settings()
    global_before = s.get("jitter_before_minutes", None)
    if global_before is None:
        global_before = s.get("jitter_minutes", 2)
    global_after = s.get("jitter_after_minutes", 0)

    if action == "in":
        before = s.get("jitter_in_before_minutes", global_before)
        after = s.get("jitter_in_after_minutes", global_after)
    elif action == "out":
        before = s.get("jitter_out_before_minutes", global_before)
        after = s.get("jitter_out_after_minutes", global_after)
    else:
        before, after = global_before, global_after

    before = max(0, int(before if before is not None else 0))
    after = max(0, int(after if after is not None else 0))
    lo = -before * 60
    hi = after * 60
    if lo == 0 and hi == 0:
        return timedelta(0)
    salt = s.get("salt", "")
    h = hashlib.sha256(f'{salt}|{iso}|{action}'.encode()).hexdigest()
    return timedelta(seconds=lo + int(h, 16) % (hi - lo + 1))


def due_at(day, hhmm, iso, action):
    hh, mm = hhmm.split(":")[:2]
    return datetime(day.year, day.month, day.day, int(hh), int(mm), 0) + jitter_offset(iso, action)


def correction_delay(iso):
    """Seconds (deterministic per user/day) to wait after clock-out before correcting,
    random within [delay_min, delay_max] seconds (default 30 .. 180 = 3 min)."""
    c = get_settings().get("correccion", {})
    lo = int(c.get("delay_min", 30))
    hi = int(c.get("delay_max", 180))
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


def tick_user():
    """Run due actions for the user in context. Return seconds until next action."""
    poll = float(get_settings().get("poll_seconds", 30))
    if not credentials_configured():
        return poll
    sched = load_json(upath("schedule.json"), {})
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
                if shift_already_has_out(iso):
                    mark_done(iso, "out")
                    if get_settings().get("correccion", {}).get("enabled", True):
                        log(f"{iso} out skipped (Woffu already auto-closed; will correct)")
                    else:
                        log(f"{iso} out skipped (Woffu already auto-closed; autocorrect off)")
                else:
                    _fire(iso, "out")

        ccfg = get_settings().get("correccion", {})
        if (same_day and out_t and ccfg.get("enabled", True)
                and is_done(iso, "in") and is_done(iso, "out") and not is_done(iso, "fix")):
            tgt = due_at(today, out_t, iso, "out") + timedelta(seconds=correction_delay(iso))
            if note(tgt):
                _fire_correction(iso)

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
                if shift_already_has_out(yiso):
                    mark_done(yiso, "out")
                    if get_settings().get("correccion", {}).get("enabled", True):
                        log(f"{yiso} out skipped (Woffu already auto-closed; will correct)")
                    else:
                        log(f"{yiso} out skipped (Woffu already auto-closed; autocorrect off)")
                else:
                    _fire(yiso, "out")

        ccfg = get_settings().get("correccion", {})
        if (crosses and ccfg.get("enabled", True)
                and is_done(yiso, "out") and not is_done(yiso, "fix")
                and now_min < yin_min):
            tgt = due_at(today, yout, yiso, "out") + timedelta(seconds=correction_delay(yiso))
            if note(tgt):
                _fire_correction(yiso)

    return max(0.2, next_wait)


def tick():
    """Run due actions for every registered user. Return sleep seconds."""
    users = list_usernames()
    if not users:
        return 30.0
    waits = []
    for username in users:
        try:
            with user_scope(username):
                waits.append(tick_user())
        except Exception as e:
            print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [{username}] tick error: {e}",
                  flush=True)
            waits.append(30.0)
    return min(waits) if waits else 30.0


def scheduler_loop():
    while True:
        try:
            wait = tick()
        except Exception as e:
            print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} scheduler error: {e}", flush=True)
            wait = 30
        time.sleep(wait)


# ----------------------------- auth helpers -----------------------------

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_app_user()
        if not user or user not in load_accounts():
            session.clear()
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect(url_for("acceso"))
        with user_scope(user):
            return fn(*args, **kwargs)
    return wrapper


# ----------------------------- routes -----------------------------

@app.route("/")
def index():
    """Decoy: amateur complaints-test page (Spanish by default)."""
    return render_template("decoy.html")


@app.route("/acceso", methods=["GET"])
def acceso():
    if current_app_user() and current_app_user() in load_accounts():
        return redirect(url_for("app_home"))
    return render_template("acceso.html")


@app.route("/api/auth/register", methods=["POST"])
def api_register():
    body = request.get_json(force=True) or {}
    username = safe_username(body.get("username"))
    password = (body.get("password") or "").strip()
    code = (body.get("code") or "").strip()
    if not username:
        return jsonify({"error": "Usuario inválido (3-32 chars: a-z, 0-9, _)."}), 400
    if len(password) < 4:
        return jsonify({"error": "Password too short (min 4)."}), 400
    if code != register_code():
        return jsonify({"error": "Invalid registration code."}), 403
    accounts = load_accounts()
    if username in accounts:
        return jsonify({"error": "Username already taken."}), 409
    accounts[username] = {
        "password_hash": generate_password_hash(password),
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    save_accounts(accounts)
    user_data_dir(username)  # create folder
    session["user"] = username
    return jsonify({"ok": True, "user": username})


@app.route("/api/auth/login", methods=["POST"])
def api_login():
    body = request.get_json(force=True) or {}
    username = safe_username(body.get("username"))
    password = (body.get("password") or "").strip()
    accounts = load_accounts()
    row = accounts.get(username) if username else None
    if not row or not check_password_hash(row.get("password_hash", ""), password):
        return jsonify({"error": "Invalid username or password."}), 401
    session["user"] = username
    return jsonify({"ok": True, "user": username})


@app.route("/api/auth/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/auth/me")
def api_me():
    user = current_app_user()
    if not user or user not in load_accounts():
        return jsonify({"user": None})
    return jsonify({"user": user})


@app.route("/app")
@login_required
def app_home():
    return render_template("index.html", app_user=current_app_user())


@app.route("/api/month/<int:year>/<int:month>")
@login_required
def api_month(year, month):
    schedule = load_json(upath("schedule.json"), {})
    state = load_json(upath("state.json"), {})
    days = {d: v for d, v in schedule.items() if d.startswith(f"{year:04d}-{month:02d}-")}
    return jsonify({
        "days": days,
        "holidays": holidays_for_month(year, month),
        "state": {d: v for d, v in state.items() if d.startswith(f"{year:04d}-{month:02d}-")},
        "today": date.today().isoformat(),
    })


@app.route("/api/day", methods=["POST"])
@login_required
def api_set_day():
    body = request.get_json(force=True)
    d = body.get("date")
    if not d:
        return jsonify({"error": "Missing date."}), 400
    schedule = load_json(upath("schedule.json"), {})
    if body.get("rest"):
        if woffu_already_rest(d):
            return jsonify({
                "error": "Weekends and official holidays are already rest in Woffu unless you add a shift.",
            }), 400
        schedule[d] = {"rest": True}
    else:
        in_t, out_t = body.get("in"), body.get("out")
        if not in_t or not out_t:
            return jsonify({"error": "Missing in or out."}), 400
        schedule[d] = {"in": in_t, "out": out_t}
    save_json(upath("schedule.json"), schedule)
    return jsonify({"ok": True, "day": schedule[d]})


@app.route("/api/month/<int:year>/<int:month>/auto-rest", methods=["POST"])
@login_required
def api_auto_rest_month(year, month):
    """Mark empty weekdays as rest. Skip weekends, official holidays, and days that already have a shift."""
    if not (1 <= month <= 12) or year < 2000 or year > 2100:
        return jsonify({"error": "Invalid month."}), 400
    schedule = load_json(upath("schedule.json"), {})
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    marked = 0
    skipped_weekend = 0
    skipped_holiday = 0
    skipped_shift = 0
    already = 0
    d = start
    while d < end:
        key = d.isoformat()
        existing = schedule.get(key)
        if is_weekend(key) or is_holiday(key):
            if is_weekend(key):
                skipped_weekend += 1
            else:
                skipped_holiday += 1
            if existing and existing.get("rest"):
                schedule.pop(key, None)
        elif existing and not existing.get("rest"):
            skipped_shift += 1
        elif existing and existing.get("rest"):
            already += 1
        else:
            schedule[key] = {"rest": True}
            marked += 1
        d += timedelta(days=1)
    save_json(upath("schedule.json"), schedule)
    return jsonify({
        "ok": True,
        "marked": marked,
        "already": already,
        "skipped_weekend": skipped_weekend,
        "skipped_holiday": skipped_holiday,
        "skipped_shift": skipped_shift,
    })


@app.route("/api/month/<int:year>/<int:month>/auto-office", methods=["POST"])
@login_required
def api_auto_office_month(year, month):
    """Fill empty weekdays with Office shift (09:00–17:00 by default).
    Skip weekends, official holidays, and days that already have a shift or rest."""
    if not (1 <= month <= 12) or year < 2000 or year > 2100:
        return jsonify({"error": "Invalid month."}), 400
    in_t, out_t = "09:00", "17:00"
    for p in get_settings().get("presets") or []:
        if (p.get("name") or "").strip().lower() == "office" and p.get("in") and p.get("out"):
            in_t, out_t = p["in"], p["out"]
            break
    schedule = load_json(upath("schedule.json"), {})
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    marked = 0
    skipped_weekend = 0
    skipped_holiday = 0
    skipped_busy = 0
    d = start
    while d < end:
        key = d.isoformat()
        existing = schedule.get(key)
        if is_weekend(key):
            skipped_weekend += 1
        elif is_holiday(key):
            skipped_holiday += 1
        elif existing:
            skipped_busy += 1
        else:
            schedule[key] = {"in": in_t, "out": out_t}
            marked += 1
        d += timedelta(days=1)
    save_json(upath("schedule.json"), schedule)
    return jsonify({
        "ok": True,
        "marked": marked,
        "in": in_t,
        "out": out_t,
        "skipped_weekend": skipped_weekend,
        "skipped_holiday": skipped_holiday,
        "skipped_busy": skipped_busy,
    })


@app.route("/api/month/<int:year>/<int:month>/clear", methods=["POST"])
@login_required
def api_clear_month(year, month):
    """Remove all shifts and rest marks for the month. Punch history is left as-is."""
    if not (1 <= month <= 12) or year < 2000 or year > 2100:
        return jsonify({"error": "Invalid month."}), 400
    prefix = f"{year:04d}-{month:02d}-"
    schedule = load_json(upath("schedule.json"), {})
    removed = [k for k in list(schedule) if k.startswith(prefix)]
    for k in removed:
        schedule.pop(k, None)
    save_json(upath("schedule.json"), schedule)
    return jsonify({"ok": True, "cleared": len(removed)})


@app.route("/api/day/<d>", methods=["DELETE"])
@login_required
def api_del_day(d):
    schedule = load_json(upath("schedule.json"), {})
    schedule.pop(d, None)
    save_json(upath("schedule.json"), schedule)
    state = load_json(upath("state.json"), {})
    state.pop(d, None)
    save_json(upath("state.json"), state)
    return jsonify({"ok": True})


@app.route("/api/settings", methods=["GET", "POST"])
@login_required
def api_settings():
    if request.method == "POST":
        s = get_settings()
        body = request.get_json(force=True) or {}
        # Deep-merge nested correccion so toggles don't wipe delay_min / out_source.
        if isinstance(body.get("correccion"), dict):
            merged = dict(s.get("correccion") or {})
            merged.update(body["correccion"])
            body = dict(body)
            body["correccion"] = merged
        s.update(body)
        save_json(upath("settings.json"), s)
        return jsonify({"ok": True, "settings": s})
    return jsonify(get_settings())


@app.route("/api/status")
@login_required
def api_status():
    iso = date.today().isoformat()
    sched = load_json(upath("schedule.json"), {}).get(iso)
    state = load_json(upath("state.json"), {}).get(iso, [])
    verified = credentials_verified()
    return jsonify({
        "credentials": credentials_configured(),
        "credentials_ok": verified is True,
        "credentials_bad": verified is False,
        "user": current_app_user(),
        "today": iso,
        "today_shift": sched,
        "today_done": state,
        "now": datetime.now().strftime("%H:%M"),
    })


@app.route("/api/credentials", methods=["POST"])
@login_required
def api_credentials():
    body = request.get_json(force=True)
    u, p = body.get("username"), body.get("password")
    if not u or not p:
        return jsonify({"error": "Missing username/password."}), 400
    save_json(upath("secrets.json"), {"username": u, "password": p, "verified": False})
    uname = getattr(_ctx, "username", None)
    if uname and uname in _token_caches:
        _token_caches[uname]["token"] = None
    return jsonify({"ok": True})


@app.route("/api/test-auth", methods=["POST"])
@login_required
def api_test_auth():
    try:
        get_token()
        domain, user_id, _ = get_ids()
        set_credentials_verified(True)
        return jsonify({"ok": True, "domain": domain, "user_id": user_id})
    except (WoffuError, requests.RequestException) as e:
        set_credentials_verified(False)
        uname = getattr(_ctx, "username", None)
        if uname and uname in _token_caches:
            _token_caches[uname]["token"] = None
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/sign-now", methods=["POST"])
@login_required
def api_sign_now():
    try:
        send_sign()
        return jsonify({"ok": True})
    except (WoffuError, requests.RequestException) as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/workday/<iso>")
@login_required
def api_workday(iso):
    """Debug: return the workday as Woffu sees it (to confirm the GET format).
    e.g. /api/workday/2026-07-13"""
    try:
        return jsonify(fetch_workday(iso))
    except (WoffuError, requests.RequestException) as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/fix-now", methods=["POST"])
@login_required
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
    host = os.environ.get("WOFFU_HOST", "0.0.0.0")
    port = int(os.environ.get("WOFFU_PORT", "40"))
    print(f"kinkyscheduler at http://{host}:{port}")
    app.run(host=host, port=port, debug=False, use_reloader=False)
