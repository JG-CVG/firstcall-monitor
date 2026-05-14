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
NEDOV_RE = re.compile(
    r"nedovol[aá]n[oý]|nebere|nicht\s+er[r]?eicht|"
    r"konnte\s+.*?nicht\s+er[r]?e[ia]cht|immer\s+noch\s+nicht",
    re.IGNORECASE,
)
P1_REJECTS = {
    "REJECT New CA", "REJECT Data Validation", "REJECT Car Check",
    "REJECT VIN Check", "REJECT Awaiting Selection",
    "QUARANTINE New CA", "QUARANTINE Car Check", "QUARANTINE VIN Check",
}
TMP = "/tmp/myrun"


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

    now_utc = datetime.now(timezone.utc)
    nedov_data = []
    nedov_count = 0
    cc_recs = [r for r in ip_recs if r["Status"] == "Car check"]
    for c in cc_recs:
        cid = c["Id"]
        cf = feeds_by_case.get(cid, [])
        nf = [f for f in cf if f.get("Body") and NEDOV_RE.search(f["Body"])]
        is_n = len(nf) > 0
        if is_n:
            nedov_count += 1
        ccd = parse_dt(c.get("CA_Car_Check_Date__c"))
        age_h = round((now_utc - ccd).total_seconds() / 3600) if ccd else None
        age_w = working_hours_between(ccd, now_utc) if ccd else None
        case_incs = [
            {
                "subject": strip_html(i.get("Subject__c", "")) or "(bez subjectu)",
                "eta": i.get("Estimated_Resolution_Date__c"),
            }
            for i in incs_by_case.get(cid, [])
        ]
        pot_close = False
        pot_span_h = None
        if age_w is not None and age_w >= 4 and len(nf) >= 2:
            ts = sorted(parse_dt(f["CreatedDate"]).timestamp() for f in nf)
            pot_span_h = round((ts[-1] - ts[0]) / 3600, 2)
            if pot_span_h >= 2:
                pot_close = True
        nedov_data.append({
            "id": cid,
            "cn": c["CaseNumber"],
            "country": vc(c),
            "agent": agents_per_case.get(cid),
            "ageH": age_h,
            "ageW": age_w,
            "isN": is_n,
            "nCnt": len(nf),
            "lastD": nf[0]["CreatedDate"] if nf else None,
            "lastBy": (nf[0].get("CreatedBy") or {}).get("Name") if nf else None,
            "incs": case_incs,
            "potClose": pot_close,
            "potSpanH": pot_span_h,
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
        "status_breakdown": sm,
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
    with open("/tmp/data.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    print(
        f"total={total} ip={ip_total} closed={closed} "
        f"cc={len(cc_recs)} nedov={nedov_count} heatmap={hm_total}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
