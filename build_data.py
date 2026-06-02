#!/usr/bin/env python3
"""
Build data.json for the 1stCall monitor dashboard from SOQL response files
saved in /tmp by a Claude scheduled task.

Inputs (all read from /tmp):
  sf_status.json          raw SOQL response for status aggregate
  sf_ip.json              raw SOQL response for In Progress records
  sf_closed_status.json   pre-aggregated dict {"REJECT New CA": 96, ...}
  sf_closed_country.json  pre-aggregated dict {"DE": 323, ...}
  sf_feeds.json           raw SOQL response for CaseFeed
  sf_incidents.json       raw SOQL response for blocking incidents
  sf_heatmap.json         raw SOQL response for heatmap aggregate

Output:
  /tmp/data.json
"""
import html as html_lib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

PRAGUE = ZoneInfo("Europe/Prague")
RE_REZERVACE = re.compile(
    r"rezervov[aá][nň]|reserviert|res[ae]viert|r[eé]serv[eé]|"
    r"reserved\s+for(\s+another)?\s+customer|"
    r"v[uů]z\s+(je\s+)?rezerv|auto\s+(je\s+)?rezerv|"
    r"fur\s+(einen\s+)?anderen\s+Kunden|"
    r"reserviert\s+f[uü]r|resaviert|reserviert",
    re.IGNORECASE,
)
NEDOV_RE = re.compile(
    r"nedovol[aá]n[oý]|nebere|nicht\s+er[r]?eicht|"
    r"konnte\s+.*?nicht\s+er[r]?e[ia]cht|immer\s+noch\s+nicht|"
    r"obsazen[oýáéí]*|besetzt|\bbusy\b|mailbox|"
    r"Verk[aä]ufer\s+ist\s+im\s+Kundengespr[aä]ch|Kundengespr[aä]ch|"
    r"sp[aä]+t?er\s+(noch\s*(ein)?\s*)?mal\s*anrufen|spaerter",
    re.IGNORECASE,
)
# Categorization of latest feed
RE_MESSAGING = re.compile(
    r"\b(e-?mail|mail|whatsapp|sms|chat|messenger)\b|"
    r"posl[au]?(m|n[ae])?\s*(mu|jej|mail|email|sms|whatsapp)|"
    r"zasl[aá]m|gesendet|geschickt|"
    r"sent\s*(an\s+)?(email|mail|sms|whatsapp)",
    re.IGNORECASE,
)
RE_CALLBACK = re.compile(
    r"zavol[aá]\s*zp[eě]t|ozv[eou]+\s*se|vr[aá]t[ií]\s+se|"
    r"meldet\s+sich|wird\s+sich\s+melden|ruft\s+zur[uü]ck|r[uü]ckruf|"
    r"kolega\s+(p[rř]i?jde|zavol[aá]|p[rř]eb[ií]r[aá])|"
    r"call(s|ed)?\s*back|callback|gets?\s+back|"
    r"\bAP\s*[:=]",
    re.IGNORECASE,
)

P1_REJECTS = {
    "REJECT New CA", "REJECT Data Validation", "REJECT Car Check",
    "REJECT VIN Check", "REJECT Awaiting Selection",
    "QUARANTINE New CA", "QUARANTINE Car Check", "QUARANTINE VIN Check",
}
TMP = os.environ.get("FCM_TMP_DIR", "/tmp")


def load(name):
    with open(os.path.join(TMP, name), "r", encoding="utf-8") as f:
        return json.load(f)


def parse_dt(s):
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    if re.search(r"[+-]\d{4}$", s):
        s = s[:-2] + ":" + s[-2:]
    return datetime.fromisoformat(s).astimezone(timezone.utc)


def working_hours_between(start_utc, end_utc):
    if start_utc is None or end_utc is None or start_utc >= end_utc:
        return 0.0
    start = start_utc.astimezone(PRAGUE)
    end = end_utc.astimezone(PRAGUE)
    total_sec = 0.0
    cur = start
    guard = 0
    while cur < end and guard < 5000:
        guard += 1
        wd = cur.weekday()
        if wd == 6:
            cur = (cur + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            continue
        oh = 8
        ch = 13 if wd == 5 else 17
        day_open = cur.replace(hour=oh, minute=0, second=0, microsecond=0)
        day_close = cur.replace(hour=ch, minute=0, second=0, microsecond=0)
        s = max(cur, day_open)
        e = min(end, day_close)
        if e > s:
            total_sec += (e - s).total_seconds()
        cur = (cur + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return round(total_sec / 3600, 2)


def strip_html(s):
    if not s:
        return ""
    return html_lib.unescape(re.sub(r"<[^>]*>", "", str(s))).strip()


def vc(rec):
    ca = rec.get("CarAudit__r") or {}
    return ca.get("Vendor_Country__c") or "N/A"


def main():
    # ---- Load inputs ----
    status_recs = load("sf_status.json")["records"]
    # All-months open cases for status_breakdown (Tier 3 display)
    try:
        status_all_recs = load("sf_status_all.json")["records"]
    except FileNotFoundError:
        status_all_recs = status_recs
    ip_recs = load("sf_ip.json")["records"]
    closed_status = load("sf_closed_status.json")
    try:
        closed_country = load("sf_closed_country.json")
    except FileNotFoundError:
        closed_country = {}
    try:
        agents_recs = load("sf_agents.json")["records"]
        agents = {}
        for r in agents_recs:
            k = r.get("Call_Center_Agent__c") or "Neprideleno"
            agents[k] = int(r.get("cnt", r.get("expr0", 0)))
    except FileNotFoundError:
        agents = {}
    try:
        with open(os.path.join(TMP, "sf_agents_per_case.json"), "r", encoding="utf-8") as f:
            agents_per_case = json.load(f)
    except FileNotFoundError:
        agents_per_case = {}
    feeds_data = load("sf_feeds.json")["records"]
    incs_data = load("sf_incidents.json")["records"]
    heatmap_recs = load("sf_heatmap.json")["records"]

    # ---- Tier 1 ----
    sm = {r["Status"]: int(r["cnt"]) for r in status_recs}
    # All-months open status map for operational calculations (buffer, etc.)
    sm_all = {r["Status"]: int(r["cnt"]) for r in status_all_recs}
    total = sum(sm.values())
    ip_statuses = ["New CA", "Data validation and completion", "Car check", "VIN Check"]
    ip_total = sum(sm.get(s, 0) for s in ip_statuses)
    closed = sm.get("CarAudit Closed", 0)
    phase2 = total - ip_total - closed

    # ---- Closed reasons (P1 only) ----
    cl_reasons = {k: v for k, v in closed_status.items() if k in P1_REJECTS}

    # ---- Country breakdown ----
    country = {}
    for r in ip_recs:
        c = vc(r)
        d = country.setdefault(c, {"ip": 0, "cl": 0, "cc": 0, "vin": 0, "newca": 0})
        d["ip"] += 1
        s = r["Status"]
        if s == "Car check":
            d["cc"] += 1
        elif s == "VIN Check":
            d["vin"] += 1
        elif s in ("New CA", "Data validation and completion"):
            d["newca"] += 1
    for c, cnt in closed_country.items():
        d = country.setdefault(c, {"ip": 0, "cl": 0, "cc": 0, "vin": 0, "newca": 0})
        d["cl"] = cnt

    # ---- Nedovolano ----
    feeds_by_case = {}
    for f in feeds_data:
        feeds_by_case.setdefault(f["ParentId"], []).append(f)
    incs_by_case = {}
    for i in incs_data:
        incs_by_case.setdefault(i["Case__c"], []).append(i)

    def categorize(body):
        """Return 'rezervace' | 'messaging' | 'callback' | 'nedov' | None for a feed body."""
        if not body:
            return None
        if RE_REZERVACE.search(body):
            return "rezervace"
        if RE_MESSAGING.search(body):
            return "messaging"
        if RE_CALLBACK.search(body):
            return "callback"
        if NEDOV_RE.search(body):
            return "nedov"
        return None

    now_utc = datetime.now(timezone.utc)
    nedov_data = []
    nedov_count = 0
    cc_recs = [r for r in ip_recs if r["Status"] == "Car check"]
    for c in cc_recs:
        cid = c["Id"]
        cf = sorted(feeds_by_case.get(cid, []), key=lambda x: x.get("CreatedDate") or "", reverse=True)
        nf = [f for f in cf if f.get("Body") and NEDOV_RE.search(f["Body"])]
        # All categorized feeds (any of 3 categories) — used for potClose and lastType
        cat_feeds = []
        for f in cf:
            cat = categorize(f.get("Body"))
            if cat:
                cat_feeds.append((cat, f))
        is_n = len(nf) > 0
        if is_n:
            nedov_count += 1
        ccd = parse_dt(c.get("CA_Car_Check_Date__c"))
        age_h = round((now_utc - ccd).total_seconds() / 3600) if ccd else None
        age_w = working_hours_between(ccd, now_utc) if ccd else None
        case_incs_raw = sorted(incs_by_case.get(cid, []), key=lambda x: x.get("CreatedDate") or "", reverse=True)
        case_incs = [
            {
                "subject": strip_html(i.get("Subject__c", "")) or "(bez subjectu)",
                "eta": i.get("Estimated_Resolution_Date__c"),
                "createdDate": i.get("CreatedDate"),
                "createdBy": (i.get("CreatedBy") or {}).get("Name"),
            }
            for i in case_incs_raw
        ]
        # potClose: 2+ contacts of ANY type, span >= 1.5h, age_w >= 4h
        pot_close = False
        pot_span_h = None
        if age_w is not None and age_w >= 4 and len(cat_feeds) >= 2:
            ts = sorted(parse_dt(f["CreatedDate"]).timestamp() for _, f in cat_feeds)
            pot_span_h = round((ts[-1] - ts[0]) / 3600, 2)
            if pot_span_h >= 1.5:
                pot_close = True
        # Last contact info — STRICT: only operators who logged a categorized feed
        # (Nedovoláno/Zavolá zpět/Email-SMS) OR created an incident. Uncategorized feed
        # noise (vendor info dumps, etc.) is ignored.
        last_type = cat_feeds[0][0] if cat_feeds else None
        last_d = None
        last_by = None
        if cat_feeds:
            last_feed = cat_feeds[0][1]
            last_d = last_feed["CreatedDate"]
            last_by = (last_feed.get("CreatedBy") or {}).get("Name")
        elif case_incs:
            # No categorized feed — fall back to incident creator
            last_d = case_incs[0].get("createdDate")
            last_by = case_incs[0].get("createdBy")
        # Incident-based rezervace override: if any blocking incident subject matches
        # and no feed already flagged rezervace, override lastType
        if last_type != "rezervace" and case_incs:
            for inc in case_incs:
                if RE_REZERVACE.search(inc.get("subject", "") or ""):
                    last_type = "rezervace"
                    if not last_d:
                        last_d = inc.get("createdDate")
                        last_by = inc.get("createdBy")
                    break
        nedov_data.append({
            "id": cid,
            "cn": c["CaseNumber"],
            "country": vc(c),
            "agent": agents_per_case.get(cid),
            "ageH": age_h,
            "ageW": age_w,
            "isN": is_n,
            "nCnt": len(nf),
            "ctCnt": len(cat_feeds),
            "lastType": last_type,
            "lastD": last_d,
            "lastBy": last_by,
            "incs": case_incs,
            "potClose": pot_close,
            "potSpanH": pot_span_h,
            "pref": bool((c.get("Order__r") or {}).get("Preferred__c")) if c.get("Order__r") else False,
        })

    # ---- Heatmap (rolling 30 days, Prague TZ) ----
    today_pg = datetime.now(PRAGUE).replace(hour=0, minute=0, second=0, microsecond=0)
    start_pg = today_pg - timedelta(days=30)
    days_per_dow = [0] * 7
    cur = start_pg
    while cur <= today_pg:
        days_per_dow[cur.weekday()] += 1
        cur += timedelta(days=1)
    matrix = [[0] * 24 for _ in range(7)]
    hm_total = 0
    for r in heatmap_recs:
        sf_dow = int(r["dow"])
        hr = int(r["hr"])
        cnt = int(r["cnt"])
        idx = (sf_dow + 5) % 7  # 1=Sun->6, 2=Mon->0, ..., 7=Sat->5
        matrix[idx][hr] = cnt
        hm_total += cnt

    # ---- Buffer hourly (Q9/Q10/Q11) — optional ----
    def _load_hourly(name):
        try:
            with open(os.path.join(TMP, name), "r", encoding="utf-8") as fh:
                return json.load(fh).get("records", [])
        except FileNotFoundError:
            return None

    bh_added = _load_hourly("sf_hourly_added.json")
    bh_vin = _load_hourly("sf_hourly_vin.json")
    bh_rej = _load_hourly("sf_hourly_rej.json")

    buffer_hourly = None
    if bh_added is not None and bh_vin is not None and bh_rej is not None:
        now_local = now_utc.astimezone(PRAGUE)
        cur_hr = now_local.hour
        # Determine Prague offset to convert UTC hours from SF
        prague_offset = int(now_local.utcoffset().total_seconds() // 3600)
        def _by_local(records):
            m = {}
            for r in records:
                hp = (int(r["hr"]) + prague_offset) % 24
                m[hp] = m.get(hp, 0) + int(r["cnt"])
            return m
        in_h = _by_local(bh_added)
        vin_h = _by_local(bh_vin)
        rej_h = _by_local(bh_rej)
        new_ca_count = sm_all.get("New CA", 0)
        cc_count = sm_all.get("Car check", 0)
        current_buffer = new_ca_count + cc_count
        sum_in = sum(in_h.get(h, 0) for h in range(8, cur_hr + 1))
        sum_vin = sum(vin_h.get(h, 0) for h in range(8, cur_hr + 1))
        sum_rej = sum(rej_h.get(h, 0) for h in range(8, cur_hr + 1))
        start_buffer = current_buffer - sum_in + sum_vin + sum_rej
        if current_buffer > 0:
            start_new_ca = max(0, round(start_buffer * new_ca_count / current_buffer))
        else:
            start_new_ca = 0
        start_car_check = max(0, start_buffer - start_new_ca)
        hours_arr = []
        running = start_buffer
        for h in range(8, 18):
            if h > cur_hr:
                hours_arr.append({"h": h, "in": None, "vin": None, "rej": None, "buffer_end": None})
                continue
            ih = in_h.get(h, 0)
            vh = vin_h.get(h, 0)
            rh = rej_h.get(h, 0)
            running = running + ih - vh - rh
            hours_arr.append({"h": h, "in": ih, "vin": vh, "rej": rh, "buffer_end": running})
        hours_elapsed = max(1, cur_hr - 8 + 1)
        net_today = sum_in - sum_vin - sum_rej
        rate_per_h = round(net_today / hours_elapsed, 2)
        hours_remaining = max(0, 17 - cur_hr)
        proj_buffer = max(0, round(current_buffer + rate_per_h * hours_remaining))
        delta_vs_now = proj_buffer - current_buffer
        buffer_hourly = {
            "start_buffer": start_buffer,
            "start_new_ca": start_new_ca,
            "start_car_check": start_car_check,
            "current_buffer": current_buffer,
            "current_new_ca": new_ca_count,
            "current_car_check": cc_count,
            "hours": hours_arr,
            "projection_18": {
                "buffer": proj_buffer,
                "rate_per_h": rate_per_h,
                "delta_vs_now": delta_vs_now,
            },
        }

    # ---- AwS customer split (Q12) ----
    def _load_aws():
        try:
            with open(os.path.join(TMP, "sf_aws_open.json"), "r", encoding="utf-8") as fh:
                return json.load(fh).get("records", [])
        except FileNotFoundError:
            return None

    aws_open_recs = _load_aws()
    aws_split = None
    if aws_open_recs is not None:
        from collections import defaultdict as _dd
        by_acc = _dd(set)
        aws_cases_cnt = 0
        for r in aws_open_recs:
            acc = r.get("AccountId")
            st = r.get("Status")
            if not acc:
                continue
            by_acc[acc].add(st)
            if st == "Awaiting Selection":
                aws_cases_cnt += 1
        aws_accs = set(a for a, s in by_acc.items() if "Awaiting Selection" in s)
        with_other = set(a for a in aws_accs if (by_acc[a] - {"Awaiting Selection"}))
        solo = aws_accs - with_other
        aws_split = {
            "cases": aws_cases_cnt,
            "customers": len(aws_accs),
            "solo": len(solo),
            "with_other": len(with_other),
        }

    # ---- Phase 2 tables (Q13) ----
    def _load_phase2():
        try:
            with open(os.path.join(TMP, "sf_phase2.json"), "r", encoding="utf-8") as fh:
                return json.load(fh).get("records", [])
        except FileNotFoundError:
            return None

    phase2_recs = _load_phase2()
    phase2_tables = None
    if phase2_recs is not None:
        P2_STATUSES = ["Auditor selection", "Audit order", "Audit result", "CarAudit preparation"]
        # Table 1: Status × Preferred / NO preferred
        pref_table = {s: {"pref": 0, "nopref": 0} for s in P2_STATUSES}
        # Table 2: Status × age bucket (working days since CA_Auditor_Selection_Date__c)
        # Buckets: <2, 2-3, 3-5, 5-7, >7 (semi-open intervals on the right)
        # Each cell now stores {pref: X, nopref: Y} for the dual-count display.
        BUCKETS = ["lt2", "b23", "b35", "b57", "gt7"]
        age_table = {s: {b: {"pref": 0, "nopref": 0} for b in BUCKETS} for s in P2_STATUSES}
        for r in phase2_recs:
            st = r.get("Status")
            if st not in P2_STATUSES:
                continue
            is_pref = bool(((r.get("Order__r") or {}).get("Preferred__c")))
            pref_table[st]["pref" if is_pref else "nopref"] += 1
            aws_dt = parse_dt(r.get("CA_Auditor_Selection_Date__c"))
            if aws_dt is None:
                continue
            wh = working_hours_between(aws_dt, now_utc)
            wd = wh / 8.0  # working hours -> working days (8 h/day)
            if wd < 2:
                b = "lt2"
            elif wd < 3:
                b = "b23"
            elif wd < 5:
                b = "b35"
            elif wd < 7:
                b = "b57"
            else:
                b = "gt7"
            age_table[st][b]["pref" if is_pref else "nopref"] += 1
        phase2_tables = {
            "statuses": P2_STATUSES,
            "preferred": pref_table,
            "age": age_table,
            "buckets": BUCKETS,
            "bucket_labels": {
                "lt2": "< 2 prac. dnů",
                "b23": "2-3 prac. dnů",
                "b35": "3-5 prac. dnů",
                "b57": "5-7 prac. dnů",
                "gt7": "> 7 prac. dnů",
            },
        }

    # ---- Audit Order timelines (Q14 = Carvago/CarAudit, Q15 = Cebia CarAudit) ----
    def _load_ao(fname):
        try:
            with open(os.path.join(TMP, fname), "r", encoding="utf-8") as fh:
                return json.load(fh).get("records", [])
        except FileNotFoundError:
            return None

    def _compute_ao_timeline(ao_recs):
        from datetime import date as _date
        DOW_CS = ["po", "út", "st", "čt", "pá", "so", "ne"]
        today_pg = datetime.now(PRAGUE).date()
        DAYS_BACK = 5
        DAYS_FWD = 7
        bucket_map = {}
        for offset in range(-DAYS_BACK, DAYS_FWD + 1):
            d = today_pg + timedelta(days=offset)
            bucket_map[d.isoformat()] = {"pref": 0, "nopref": 0, "countries": {}, "cases": []}
        overflow_back = {"pref": 0, "nopref": 0}
        overflow_fwd = {"pref": 0, "nopref": 0}
        for r in ao_recs:
            ca = r.get("CarAudit__r") or {}
            pdd = ca.get("Promised_Delivery_Date__c")
            if not pdd:
                continue
            dt = parse_dt(pdd)
            if dt is None:
                continue
            d = dt.astimezone(PRAGUE).date()
            is_pref = bool(((r.get("Order__r") or {}).get("Preferred__c")))
            cc = ca.get("Vendor_Country__c") or "N/A"
            cid = r.get("Id")
            cn = r.get("CaseNumber") or ""
            key = d.isoformat()
            if key in bucket_map:
                b = bucket_map[key]
                b["pref" if is_pref else "nopref"] += 1
                b["countries"][cc] = b["countries"].get(cc, 0) + 1
                b["cases"].append({"id": cid, "cn": cn, "country": cc, "pref": is_pref})
            elif d < today_pg + timedelta(days=-DAYS_BACK):
                overflow_back["pref" if is_pref else "nopref"] += 1
            else:
                overflow_fwd["pref" if is_pref else "nopref"] += 1
        days = []
        for offset in range(-DAYS_BACK, DAYS_FWD + 1):
            d = today_pg + timedelta(days=offset)
            b = bucket_map[d.isoformat()]
            if offset < 0:
                status = "overdue"
            elif offset == 0:
                status = "today"
            else:
                status = "future"
            days.append({
                "date": d.isoformat(),
                "label": f"{d.day}.{d.month}.",
                "dow": DOW_CS[d.weekday()],
                "offset": offset,
                "status": status,
                "pref": b["pref"],
                "nopref": b["nopref"],
                "total": b["pref"] + b["nopref"],
                "countries": b["countries"],
                "cases": b["cases"],
            })
        overdue_total = sum(x["total"] for x in days if x["status"] == "overdue")
        overdue_pref = sum(x["pref"] for x in days if x["status"] == "overdue")
        today_day = next(x for x in days if x["status"] == "today")
        tomorrow_day = next((x for x in days if x["offset"] == 1), None)
        week_total = sum(x["total"] for x in days if 0 <= x["offset"] <= 6)
        max_count = max([x["total"] for x in days] + [1])
        max_overdue_offset = 0
        for x in days:
            if x["status"] == "overdue" and x["total"] > 0:
                max_overdue_offset = max(max_overdue_offset, -x["offset"])
        return {
            "today_iso": today_pg.isoformat(),
            "days_back": DAYS_BACK,
            "days_fwd": DAYS_FWD,
            "max_count": max_count,
            "days": days,
            "overflow_back": overflow_back,
            "overflow_fwd": overflow_fwd,
            "totals": {
                "overdue": overdue_total,
                "overdue_pref": overdue_pref,
                "overdue_days_count": sum(1 for x in days if x["status"] == "overdue" and x["total"] > 0),
                "max_overdue_days_back": max_overdue_offset,
                "today": today_day["total"],
                "today_pref": today_day["pref"],
                "tomorrow": tomorrow_day["total"] if tomorrow_day else 0,
                "tomorrow_pref": tomorrow_day["pref"] if tomorrow_day else 0,
                "week": week_total,
                "total": sum(x["total"] for x in days) + overflow_back["pref"] + overflow_back["nopref"] + overflow_fwd["pref"] + overflow_fwd["nopref"],
            },
        }

    ao_recs = _load_ao("sf_audit_order_expected.json")
    audit_order_expected = _compute_ao_timeline(ao_recs) if ao_recs is not None else None
    cebia_recs = _load_ao("sf_cebia_audit_order_expected.json")
    cebia_audit_order_expected = _compute_ao_timeline(cebia_recs) if cebia_recs is not None else None

    # ---- Prep → Done/Closed daily heatmap (Q16) ----
    def _load_ptd():
        try:
            with open(os.path.join(TMP, "sf_prep_to_done.json"), "r", encoding="utf-8") as fh:
                return json.load(fh).get("records", [])
        except FileNotFoundError:
            return None

    ptd_recs = _load_ptd()
    prep_to_done_daily = None
    if ptd_recs is not None:
        from datetime import date as _date
        DOW_CS = ["po", "út", "st", "čt", "pá", "so", "ne"]
        today_pg = datetime.now(PRAGUE).date()
        DAYS_BACK_WORKING = 14
        days_list = []
        cur = today_pg
        while len(days_list) < DAYS_BACK_WORKING:
            if cur.weekday() < 5:
                days_list.append(cur)
            cur = cur - timedelta(days=1)
        date_keys = set(d.isoformat() for d in days_list)
        agg = {"CarAudit": {}, "Cebia CarAudit": {}}
        for r in ptd_recs:
            rt = r.get("RecordType")
            if rt not in agg:
                continue
            name = r.get("CreatedByName") or "Unknown"
            cd_str = r.get("CreatedDate")
            if not cd_str:
                continue
            cd = parse_dt(cd_str)
            if cd is None:
                continue
            d = cd.astimezone(PRAGUE).date()
            dk = d.isoformat()
            if dk not in date_keys:
                continue
            user_map = agg[rt].setdefault(name, {})
            user_map[dk] = user_map.get(dk, 0) + 1
        def _section(rt):
            users_data = agg[rt]
            users = []
            for name, dm in users_data.items():
                row = {"name": name, "by_date": {}, "total": 0}
                for d in days_list:
                    dk = d.isoformat()
                    cnt = dm.get(dk, 0)
                    row["by_date"][dk] = cnt
                    row["total"] += cnt
                users.append(row)
            users.sort(key=lambda r: r["total"], reverse=True)
            day_totals = []
            for d in days_list:
                dk = d.isoformat()
                day_totals.append(sum(u["by_date"][dk] for u in users))
            max_cell = max([max(u["by_date"].values()) for u in users] + [0]) if users else 0
            return {"users": users, "day_totals": day_totals, "max_cell": max_cell}
        prep_to_done_daily = {
            "today_iso": today_pg.isoformat(),
            "days": [{"date": d.isoformat(), "label": f"{d.day}.{d.month}.", "dow": DOW_CS[d.weekday()]} for d in days_list],
            "carvago": _section("CarAudit"),
            "cebia": _section("Cebia CarAudit"),
        }

    # ---- SANITY CHECK — fail-fast pokud filtry chybí v Q1 ----
    # Carvago květen 2026 by mělo mít ~1 300-1 700 cases. 5000+ = filtr chybí (Q1 bez THIS_MONTH/Instamotion/Vendor)
    if total > 5000:
        raise SystemExit(
            f"\n\n🛑 SANITY CHECK FAILED: total={total} (max 5000 expected for Carvago monthly).\n"
            f"Probable cause: Q1 ran without filters (CA_New_CarAudit_Date__c=THIS_MONTH AND "
            f"Order__r.Instamotion_Customer__c=false AND CarAudit__r.Vendor_Country__c NOT IN ('XK','AL')).\n"
            f"Fix: Re-run Q1 with correct filters per SKILL.md §6.\n"
            f"Build aborted to prevent bad data deployment.\n"
        )
    if ip_total > 300:
        raise SystemExit(f"\n\n🛑 SANITY CHECK FAILED: ip_total={ip_total} (max 300 expected). Filtr chybí v Q1.\n")
    if closed > 3000:
        raise SystemExit(f"\n\n🛑 SANITY CHECK FAILED: closed={closed} (max 3000 expected/month). Filtr chybí v Q1.\n")

    # ---- Output ----
    output = {
        "schema_version": 1,
        "generated_at_utc": now_utc.isoformat(),
        "generated_at_local": now_utc.astimezone(PRAGUE).isoformat(),
        "sf_base_url": "https://carvago.lightning.force.com",
        "month_label": now_utc.astimezone(PRAGUE).strftime("%B %Y"),
        "tier1": {
            "total": total, "closed": closed, "phase2": phase2, "ip_total": ip_total,
        },
        "status_breakdown": {r["Status"]: int(r["cnt"]) for r in status_all_recs},
        "closed_reasons": cl_reasons,
        "country_breakdown": country,
        "agent_breakdown": agents,
        "nedovolano": nedov_data,
        "nedov_count": nedov_count,
        "heatmap": {
            "matrix": matrix,
            "days_per_dow": days_per_dow,
            "total_cases": hm_total,
            "start_date": start_pg.strftime("%Y-%m-%d"),
            "end_date": today_pg.strftime("%Y-%m-%d"),
        },
    }
    if buffer_hourly is not None:
        output["buffer_hourly"] = buffer_hourly
    if aws_split is not None:
        output["aws_split"] = aws_split
    if phase2_tables is not None:
        output["phase2_tables"] = phase2_tables
    if audit_order_expected is not None:
        output["audit_order_expected"] = audit_order_expected
    if cebia_audit_order_expected is not None:
        output["cebia_audit_order_expected"] = cebia_audit_order_expected
    if prep_to_done_daily is not None:
        output["prep_to_done_daily"] = prep_to_done_daily
    with open("/tmp/data.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    print(
        f"total={total} ip={ip_total} closed={closed} "
        f"cc={len(cc_recs)} nedov={nedov_count} heatmap={hm_total}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
