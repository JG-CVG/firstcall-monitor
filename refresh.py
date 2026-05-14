#!/usr/bin/env python3
"""
1st Call Monitor — data refresh script.

Connects to Salesforce, runs all queries the dashboard needs, and writes
data.json next to this script. Designed to run on a 10-minute cron via
GitHub Actions.

Env vars required:
  SF_USERNAME       Service user username (e.g. dashboard@carvago.com)
  SF_PASSWORD       Service user password
  SF_TOKEN          Salesforce security token
  SF_DOMAIN         Optional. 'login' (prod, default) or 'test' (sandbox).
  SF_BASE_URL       Optional. Lightning base URL for case links.
                    Default: https://carvago.lightning.force.com
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from simple_salesforce import Salesforce

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SF_USERNAME = os.environ["SF_USERNAME"]
SF_PASSWORD = os.environ["SF_PASSWORD"]
SF_TOKEN = os.environ["SF_TOKEN"]
SF_DOMAIN = os.environ.get("SF_DOMAIN", "login")  # 'login' or 'test'
SF_BASE_URL = os.environ.get("SF_BASE_URL", "https://carvago.lightning.force.com")

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

IP_STATUSES = ["New CA", "Data validation and completion", "Car check", "VIN Check"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def soql(sf: Salesforce, q: str) -> list:
    """Run a SOQL query, transparently handling pagination."""
    return sf.query_all(q)["records"]


def parse_dt(s):
    """Parse a Salesforce ISO datetime ('2026-05-13T08:00:00.000+0000')."""
    if not s:
        return None
    # simple_salesforce returns the string verbatim
    s = s.replace("Z", "+00:00")
    # SF sometimes returns +0000 without colon — normalise
    if re.search(r"[+-]\d{4}$", s):
        s = s[:-2] + ":" + s[-2:]
    return datetime.fromisoformat(s).astimezone(timezone.utc)


def vc(rec):
    """Vendor country off a Case record (from CarAudit__r)."""
    ca = rec.get("CarAudit__r") or {}
    return ca.get("Vendor_Country__c") or "N/A"


def strip_html(s):
    if not s:
        return ""
    import html as html_lib
    text = re.sub(r"<[^>]*>", "", str(s))
    return html_lib.unescape(text).strip()


def working_hours_between(start_utc: datetime, end_utc: datetime) -> float:
    """
    Pracovni hodiny mezi dvema okamziky.
    Po-Pa 08:00-17:00 (Prague), So 08:00-13:00, Ne zavreno.
    """
    if start_utc is None or end_utc is None or start_utc >= end_utc:
        return 0.0
    start = start_utc.astimezone(PRAGUE)
    end = end_utc.astimezone(PRAGUE)
    total_seconds = 0.0
    cursor = start
    guard = 0
    while cursor < end and guard < 5000:
        guard += 1
        wd = cursor.weekday()  # 0=Mon..6=Sun
        if wd == 6:  # Sunday — skip whole day
            next_day = (cursor + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            cursor = next_day
            continue
        open_hour = 8
        close_hour = 13 if wd == 5 else 17
        day_open = cursor.replace(hour=open_hour, minute=0, second=0, microsecond=0)
        day_close = cursor.replace(hour=close_hour, minute=0, second=0, microsecond=0)
        int_start = max(cursor, day_open)
        int_end = min(end, day_close)
        if int_end > int_start:
            total_seconds += (int_end - int_start).total_seconds()
        cursor = (cursor + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    return round(total_seconds / 3600, 2)


def month_range_utc():
    """Aktualni mesic v UTC pro WHERE klauzuli."""
    now = datetime.now(timezone.utc)
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if first.month == 12:
        nxt = first.replace(year=first.year + 1, month=1)
    else:
        nxt = first.replace(month=first.month + 1)
    return first.strftime("%Y-%m-%dT%H:%M:%SZ"), nxt.strftime("%Y-%m-%dT%H:%M:%SZ")


def base_where(first_iso, next_iso):
    return (
        f"RecordType.Name='CarAudit' "
        f"AND CA_New_CarAudit_Date__c>={first_iso} "
        f"AND CA_New_CarAudit_Date__c<{next_iso} "
        f"AND Order__r.Instamotion_Customer__c=false "
        f"AND CarAudit__r.Vendor_Country__c NOT IN ('XK','AL')"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    print(f"[{datetime.now(PRAGUE).isoformat(timespec='seconds')}] Connecting to Salesforce…")
    sf = Salesforce(
        username=SF_USERNAME,
        password=SF_PASSWORD,
        security_token=SF_TOKEN,
        domain=SF_DOMAIN,
    )

    first_iso, next_iso = month_range_utc()
    w = base_where(first_iso, next_iso)

    # 1) Status aggregate
    print("→ Status agg…")
    status_recs = soql(
        sf, f"SELECT Status, COUNT(Id) cnt FROM Case WHERE {w} GROUP BY Status"
    )
    sm = {r["Status"]: int(r["cnt"]) for r in status_recs}
    total = sum(sm.values())
    ip_total = sum(sm.get(s, 0) for s in IP_STATUSES)
    closed = sm.get("CarAudit Closed", 0)
    phase2 = total - ip_total - closed

    # 2) In Progress records
    print("→ IP records…")
    ip_recs = soql(
        sf,
        f"SELECT Id, CaseNumber, Status, CarAudit__r.Vendor_Country__c, "
        f"CA_Car_Check_Date__c FROM Case WHERE {w} "
        f"AND Status IN ('New CA','Data validation and completion','Car check','VIN Check') "
        f"LIMIT 500",
    )

    # 3) Closed records
    print("→ Closed records…")
    cl_recs = soql(
        sf,
        f"SELECT Id, CaseNumber, CarAudit_Status__c, CarAudit__r.Vendor_Country__c "
        f"FROM Case WHERE {w} AND Status='CarAudit Closed' LIMIT 2000",
    )
    cl_reasons = {}
    for r in cl_recs:
        st = r.get("CarAudit_Status__c") or "Unknown"
        if st in P1_REJECTS:
            cl_reasons[st] = cl_reasons.get(st, 0) + 1

    # 4) Country breakdown
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
    for r in cl_recs:
        c = vc(r)
        d = country.setdefault(c, {"ip": 0, "cl": 0, "cc": 0, "vin": 0, "newca": 0})
        d["cl"] += 1

    # 5) Car Check cases — CaseFeed (nedovolano detection) + blocking incidents
    cc_recs = [r for r in ip_recs if r["Status"] == "Car check"]
    feeds_by_case = {}
    incs_by_case = {}
    if cc_recs:
        ids = ",".join("'{}'".format(r["Id"]) for r in cc_recs)
        print(f"→ CaseFeed for {len(cc_recs)} car-check cases…")
        feeds = soql(
            sf,
            f"SELECT ParentId, Body, CreatedDate, CreatedBy.Name FROM CaseFeed "
            f"WHERE ParentId IN ({ids}) AND Type='TextPost' "
            f"ORDER BY CreatedDate DESC LIMIT 1000",
        )
        for f in feeds:
            feeds_by_case.setdefault(f["ParentId"], []).append(f)
        print("→ Blocking incidents…")
        incs = soql(
            sf,
            f"SELECT Case__c, Subject__c, Estimated_Resolution_Date__c FROM Incident__c "
            f"WHERE Case__c IN ({ids}) AND Type__c='blocking' AND Status__c='open' "
            f"ORDER BY CreatedDate DESC LIMIT 500",
        )
        for i in incs:
            incs_by_case.setdefault(i["Case__c"], []).append(i)

    now_utc = datetime.now(timezone.utc)
    nedov_data = []
    nedov_count = 0
    for c in cc_recs:
        cid = c["Id"]
        case_feeds = feeds_by_case.get(cid, [])
        nf = [f for f in case_feeds if f.get("Body") and NEDOV_RE.search(f["Body"])]
        is_n = len(nf) > 0
        if is_n:
            nedov_count += 1

        ccd = parse_dt(c.get("CA_Car_Check_Date__c"))
        age_h = round((now_utc - ccd).total_seconds() / 3600) if ccd else None
        age_w = working_hours_between(ccd, now_utc) if ccd else None

        country_code = vc(c)
        last_d = nf[0]["CreatedDate"] if nf else None
        last_by = (nf[0].get("CreatedBy") or {}).get("Name") if nf else None

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
            "country": country_code,
            "ageH": age_h,
            "ageW": age_w,
            "isN": is_n,
            "nCnt": len(nf),
            "lastD": last_d,
            "lastBy": last_by or "—",
            "incs": case_incs,
            "potClose": pot_close,
            "potSpanH": pot_span_h,
        })

    # 6) Heatmap aggregate — rolling 30 days, Prague TZ via convertTimezone
    print("→ Heatmap agg (rolling 30 days)…")
    today_pg = datetime.now(PRAGUE).replace(hour=0, minute=0, second=0, microsecond=0)
    start_pg = today_pg - timedelta(days=30)
    start_iso = start_pg.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    days_per_dow = [0] * 7
    cur = start_pg
    while cur <= today_pg:
        days_per_dow[cur.weekday()] += 1
        cur += timedelta(days=1)

    hm_recs = soql(
        sf,
        f"SELECT DAY_IN_WEEK(convertTimezone(CA_New_CarAudit_Date__c)) dow, "
        f"HOUR_IN_DAY(convertTimezone(CA_New_CarAudit_Date__c)) hr, "
        f"COUNT(Id) cnt FROM Case "
        f"WHERE RecordType.Name='CarAudit' "
        f"AND CA_New_CarAudit_Date__c >= {start_iso} "
        f"AND CA_New_CarAudit_Date__c <= {end_iso} "
        f"AND Order__r.Instamotion_Customer__c=false "
        f"AND Is_TEST_Case__c=false "
        f"AND Country_Origin__c NOT IN ('XK','AL') "
        f"GROUP BY DAY_IN_WEEK(convertTimezone(CA_New_CarAudit_Date__c)), "
        f"HOUR_IN_DAY(convertTimezone(CA_New_CarAudit_Date__c))",
    )
    # Build 7x24 matrix in display order Po..Ne
    matrix = [[0] * 24 for _ in range(7)]
    hm_total = 0
    for r in hm_recs:
        sf_dow = int(r["dow"])
        hr = int(r["hr"])
        cnt = int(r["cnt"])
        idx = (sf_dow + 5) % 7  # 1(Sun)->6, 2(Mon)->0, …, 7(Sat)->5
        matrix[idx][hr] = cnt
        hm_total += cnt

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------
    output = {
        "schema_version": 1,
        "generated_at_utc": now_utc.isoformat(),
        "generated_at_local": now_utc.astimezone(PRAGUE).isoformat(),
        "sf_base_url": SF_BASE_URL,
        "month_label": now_utc.astimezone(PRAGUE).strftime("%B %Y"),
        "tier1": {
            "total": total,
            "closed": closed,
            "phase2": phase2,
            "ip_total": ip_total,
        },
        "status_breakdown": sm,
        "closed_reasons": cl_reasons,
        "country_breakdown": country,
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

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)

    print(
        f"Done. total={total} ip={ip_total} closed={closed} "
        f"cc_cases={len(cc_recs)} nedov={nedov_count} heatmap_cases={hm_total}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
