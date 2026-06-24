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
    r"\bAP\s*[:=]|"
    r"sp[aä]+t?er\s+anrufen|spaerter|am\s+(?:morgen|nachmittag|montag|dienstag|mittwoch|donnerstag|freitag)\s+anrufen|morgen\s+anrufen|jindy|pozdeji|hodinu|"
    r"(?:fin|vin|gutachten|info|infos|informace)\s*(?:posila|posílá|sendet|wird\s+gesendet|geschickt|schickt|per\s+mail|per\s+wa|per\s+whatsapp)|"
    r"sendet\s+(?:alles|sobald)|hat\s+mich\s+gebeten\s+alle\s+fragen\s+per\s+mail|"
    r"zaneprázdněn|moment[aá]ln[ěe]\s+zaneprázdněn",
    re.IGNORECASE,
)

# 6-category extension (2026-06-16) — pro lastCategory field v nedovolano detail
# "Vuz prodan" = voz JE prodan (sold). Oddeleno od "Nelze prodat" (Josef 2026-06-17).
RE_PRODANO = re.compile(
    r"(?:auto|v[uů]z|fahrzeug|car|wagen)\s*(?:je|ist|wurde|already|uz|už)?\s*(?:prod[aá]n[aoeéyý]*|verkauft|sold)|"
    r"prod[aá]n[aoeéyý]*|verkauft|\bsold\b|already\s+sold|odprod[aá]n[aoy]*|verkoupen",
    re.IGNORECASE,
)
# "Nelze prodat" = voz nelze prodat / neni k dispozici / bez zajmu / jen koncovy zakaznik.
RE_NELZE_PRODAT = re.compile(
    r"nen[ií]\s*(?:k\s*dispozici|available|st[aá]le\s+k\s+dispozici)|"
    r"nicht\s*(?:mehr\s+)?verf[uü]gbar|not\s+available|not\s+anymore|"
    r"kein(?:e|en)?\s+interes+e|nem[aá]\s+z[aá]jem|no\s+interest|hat\s+keine\s+interesse|"
    r"kann\s+nicht\s+an\s+H[aä]ndler|nur\s+an\s+Endkunde|kein\s+gutachten|gutachten\s+verweigert",
    re.IGNORECASE,
)
RE_NECEKAN_FYZICKY = re.compile(
    r"je\s+na\s+cest[ěe]\b|nedorazi[lo]|je[št][ěe]\s+nen[ií]\s+(?:na|v|u\s+n[aá]s)|"
    r"noch\s+nicht\s+(?:da|angekommen|angeliefert|im\s+lager|im\s+depot)|"
    r"noch\s+(?:auf|in)\s+transport(?:er)?|in\s+transit|on\s+the\s+way|chyb[ií]\s+fyzicky|im\s+transport",
    re.IGNORECASE,
)

# Systemove feed zaznamy, ktere se NEPOCITAJI jako pokus o kontakt ani jako "Posledni pokus"
# (Josef 2026-06-17): "DO feed se nepocitaji zaznamy Case status updated a Case created"
RE_SYSTEM_FEED = re.compile(
    r"case\s+status\s+updated|case\s+created|"
    r"changed\s+status\s+from|status\s+changed\s+from|"
    r"stav\s+p[rř][ií]padu\s+(?:zm[eě]n[eě]n|aktualiz)|p[rř][ií]pad\s+vytvo[rř]en",
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


def working_hours_mf(start_utc, end_utc):
    """Pracovni hodiny POUZE Po-Pa 08:00-17:00 (Europe/Prague), bez soboty/nedele.
    Pouziva se LOKALNE v Car Check 'nedovolano detail' tabulce (b5 Prac. hod. + b9 odstup
    mezi kontakty). Josef 2026-06-17: jen pro tuto tabulku, zbytek dashboardu nemenit."""
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
        if wd >= 5:  # sobota / nedele se nepocita
            cur = (cur + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            continue
        day_open = cur.replace(hour=8, minute=0, second=0, microsecond=0)
        day_close = cur.replace(hour=17, minute=0, second=0, microsecond=0)
        ss = max(cur, day_open)
        ee = min(end, day_close)
        if ee > ss:
            total_sec += (ee - ss).total_seconds()
        cur = (cur + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return round(total_sec / 3600, 2)


def strip_html(s):
    if not s:
        return ""
    return html_lib.unescape(re.sub(r"<[^>]*>", "", str(s))).strip()


def vc(rec):
    ca = rec.get("CarAudit__r") or {}
    return ca.get("Vendor_Country__c") or "N/A"


def compute_agent_activity(move_recs, feed_recs, now_utc):
    """Aktivita agentu DNES po hodinach. Radky = Call Center Agent case (prazdne -> 'Neprideleno').
    Sloupce = hodiny dne (Europe/Prague). Bunka = pocet "logu" daneho agenta v te hodine.

    Log = jedna ze dvou udalosti behem dneska:
      (move) Case presel Car check -> VIN Check  (CA_VIN_Check_Date__c = TODAY)
      (feed) CaseFeed log jineho typu nez 'Case Created' (CreateRecordEvent) a 'Case status
             updated' (ChangeStatusPost) -> tj. TextPost / EmailMessageEvent / LinkPost ...

    Vstup = dva agregovane SOQL vystupy (agent, hr, cnt). Hodina je uz v Prague TZ
    (HOUR_IN_DAY(convertTimezone(...))). Vraci dict nebo None pokud nejsou zadna data.
    """
    if not move_recs and not feed_recs:
        return None
    from collections import defaultdict
    by_hour = defaultdict(lambda: defaultdict(int))
    move_by = defaultdict(int)
    feed_by = defaultdict(int)
    hours_seen = set()

    def _ingest(recs, per_agent):
        tot = 0
        for r in (recs or []):
            hr = r.get("hr")
            if hr is None:
                continue
            hr = int(hr)
            ag = r.get("agent") or "Neprideleno"
            cnt = int(r.get("cnt", r.get("expr0", 0)))
            if cnt <= 0:
                continue
            by_hour[ag][hr] += cnt
            per_agent[ag] += cnt
            hours_seen.add(hr)
            tot += cnt
        return tot

    move_total = _ingest(move_recs, move_by)
    feed_total = _ingest(feed_recs, feed_by)
    if not by_hour:
        return None

    hours = sorted(set(range(7, 19)) | hours_seen)
    rows = []
    for ag, hm in by_hour.items():
        rows.append({
            "agent": ag,
            "by_hour": {str(h): c for h, c in hm.items()},
            "total": sum(hm.values()),
            "move": move_by.get(ag, 0),
            "feed": feed_by.get(ag, 0),
        })
    rows.sort(key=lambda x: (x["agent"] == "Neprideleno", -x["total"], x["agent"]))
    col_totals = {str(h): sum(hm.get(h, 0) for hm in by_hour.values()) for h in hours}
    now_pg = now_utc.astimezone(PRAGUE)
    return {
        "today_iso": now_pg.strftime("%Y-%m-%d"),
        "current_hour": now_pg.hour,
        "hours": hours,
        "rows": rows,
        "col_totals": col_totals,
        "grand_total": move_total + feed_total,
        "src_move_total": move_total,
        "src_feed_total": feed_total,
    }


def compute_capacity_reco(tp_vin, tp_rej, buffer_hourly, now_utc):
    """Capacity recommendation (predikce CA). Prahy odsouhlaseny 2026-06-11 (Josef):
    kapacita 9-10 CA/os./den, spread 2 CA/den, presčas->nabor 1.5 h/os./den,
    baseline okno = dokoncene vsedni dny v poslednich 7 dnech. Vraci dict nebo None."""
    if tp_vin is None or tp_rej is None or buffer_hourly is None:
        return None
    import math
    CAP_LOW, CAP_HIGH = 9.0, 10.0
    CAP_MID = (CAP_LOW + CAP_HIGH) / 2.0
    NET_HOURS = 8.0
    SPREAD_THRESHOLD = 2.0
    OT_THRESHOLD_H = 1.5

    cr_now_local = now_utc.astimezone(PRAGUE)
    today_str = cr_now_local.strftime("%Y-%m-%d")

    per_agent_day = {}
    for rec in (tp_vin + tp_rej):
        ag = rec.get("Call_Center_Agent__c") or "Neprideleno"
        d = rec.get("d")
        if not d:
            continue
        d = str(d)[:10]
        cnt = int(rec.get("cnt", rec.get("expr0", 0)))
        per_agent_day.setdefault(ag, {})
        per_agent_day[ag][d] = per_agent_day[ag].get(d, 0) + cnt

    all_dates = set()
    for dd in per_agent_day.values():
        all_dates.update(dd.keys())

    def _is_weekday(ds):
        try:
            return datetime.strptime(ds, "%Y-%m-%d").weekday() < 5
        except ValueError:
            return False

    baseline_dates = sorted(d for d in all_dates if d != today_str and _is_weekday(d))
    divisor = len(baseline_dates) or 1

    # aktivni zpracovatele: vyradi Neprideleno a "sumove" agenty (aktivni < 2 vsedni dny a 0 dnes)
    agent_stats = {}
    for ag, dd in per_agent_day.items():
        if ag in ("Neprideleno", None):
            continue
        base_total = sum(dd.get(d, 0) for d in baseline_dates)
        today_total = dd.get(today_str, 0)
        active_days = sum(1 for d in baseline_dates if dd.get(d, 0) > 0)
        if active_days < 2 and today_total == 0:
            continue
        agent_stats[ag] = {"baseline": round(base_total / divisor, 1), "today": today_total}

    active = sorted(agent_stats.keys())
    N = len(active)
    if N == 0:
        return None

    baselines = [agent_stats[a]["baseline"] for a in active]
    team_avg = round(sum(baselines) / N, 1)
    spread = round(max(baselines) - min(baselines), 1)
    laggards = [{"agent": a, "baseline": agent_stats[a]["baseline"]}
                for a in active if agent_stats[a]["baseline"] < CAP_LOW]
    potential_gain = round(sum(CAP_MID - agent_stats[a]["baseline"]
                               for a in active if agent_stats[a]["baseline"] < CAP_LOW), 1)
    uniform = (spread <= SPREAD_THRESHOLD) and not laggards
    outliers = [a for a in active if agent_stats[a]["baseline"] > 2 * CAP_HIGH]
    note = ""
    if outliers:
        note = ("Pozn.: " + ", ".join(outliers) + " překračuje 2× modelový strop (>"
                + str(int(2 * CAP_HIGH)) + " CA/den) — reálná kapacita týmu může být vyšší "
                "než model 9–10/os., skutečný deficit proto může být menší.")

    current_buffer = buffer_hourly["current_buffer"]
    hrs = buffer_hourly["hours"]
    sum_in = sum(h["in"] for h in hrs if h["in"] is not None)
    elapsed = len([h for h in hrs if h["in"] is not None]) or 1
    remaining = len([h for h in hrs if h["in"] is None])
    incoming_rest = round((sum_in / elapsed) * remaining)
    processed_today = sum(agent_stats[a]["today"] for a in active)
    demand_remaining = current_buffer + incoming_rest
    demand_today = processed_today + demand_remaining

    cap_mid_total = N * CAP_MID
    gap_mid = round(demand_today - cap_mid_total, 1)
    extra_low = max(0.0, demand_today - N * CAP_LOW)
    extra_high = max(0.0, demand_today - N * CAP_HIGH)
    extra_mid = max(0.0, demand_today - cap_mid_total)
    ot_total_low = round(extra_low / (CAP_LOW / NET_HOURS), 1)
    ot_total_high = round(extra_high / (CAP_HIGH / NET_HOURS), 1)
    ot_pp_low = round(ot_total_low / N, 1)
    ot_pp_high = round(ot_total_high / N, 1)
    ot_pp_mid = (extra_mid / (CAP_MID / NET_HOURS)) / N
    add_low = math.ceil(extra_low / CAP_LOW) if extra_low > 0 else 0
    add_high = math.ceil(extra_high / CAP_HIGH) if extra_high > 0 else 0

    if gap_mid <= 0:
        level, label = 0, "OK"
        headline = (f"✅ Kapacita stačí — {N} zpracovatelů pokryje dnešní objem "
                    f"(~{round(demand_today)} CA vs kapacita {int(N*CAP_LOW)}–{int(N*CAP_HIGH)}).")
    elif laggards and potential_gain >= gap_mid:
        level, label = 1, "Produktivita"
        names = ", ".join(l["agent"] for l in laggards)
        headline = (f"⚠ Zařiď produktivitu týmu — {len(laggards)} pod pásmem 9–10 CA/den "
                    f"({names}). Srovnáním získáš ~{potential_gain:g} CA/den, "
                    f"pokryje dnešní deficit (~{round(gap_mid)} CA).")
    elif ot_pp_mid <= OT_THRESHOLD_H:
        level, label = 2, "Přesčas"
        headline = (f"⏱ Při tomto tempu potřeba ~{ot_pp_low:g}–{ot_pp_high:g} h přesčasu "
                    f"na osobu ({ot_total_low:g}–{ot_total_high:g} h celkem) — chybí ~{round(gap_mid)} CA.")
    else:
        level, label = 3, "Nábor"
        headline = (f"🚨 Nutné doplnit lidi — tým je produktivní, ale i při plné kapacitě "
                    f"chybí ~{round(gap_mid)} CA/den. Doporučeno +{add_high}–{add_low} zpracovatel(é).")

    return {
        "level": level, "label": label, "headline": headline,
        "n_agents": N,
        "cap_low": CAP_LOW, "cap_high": CAP_HIGH, "net_hours": NET_HOURS,
        "team_capacity_low": int(N * CAP_LOW), "team_capacity_high": int(N * CAP_HIGH),
        "processed_today": processed_today,
        "current_buffer": current_buffer,
        "incoming_rest": incoming_rest,
        "demand_remaining": demand_remaining,
        "demand_today": round(demand_today),
        "gap_mid": gap_mid,
        "team_avg": team_avg, "spread": spread, "uniform": uniform,
        "laggards": laggards, "potential_gain": potential_gain,
        "overtime": {
            "per_person_low": ot_pp_low, "per_person_high": ot_pp_high,
            "total_low": ot_total_low, "total_high": ot_total_high,
        },
        "add_people_low": add_low, "add_people_high": add_high,
        "window_days": divisor,
        "outlier_agents": outliers,
        "note": note,
        "agents": [
            {"agent": a, "baseline": agent_stats[a]["baseline"],
             "today": agent_stats[a]["today"],
             "band": ("below" if agent_stats[a]["baseline"] < CAP_LOW
                      else "above" if agent_stats[a]["baseline"] > CAP_HIGH else "in")}
            for a in sorted(active, key=lambda x: agent_stats[x]["baseline"], reverse=True)
        ],
    }


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
        """Legacy 4-kategorie: 'rezervace' | 'messaging' | 'callback' | 'nedov' | None. Drží potClose logiku + lastType field."""
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

    def categorize_full(body):
        """6-kategorie pro lastCategory (2026-06-16): prodano | rezervovano | necekan_fyzicky |
        zavolejte_jindy | nedovolano | ostatni. Priority: terminal states first."""
        if not body:
            return "ostatni"
        if RE_PRODANO.search(body): return "prodano"
        if RE_NELZE_PRODAT.search(body): return "nelze_prodat"
        if RE_REZERVACE.search(body): return "rezervovano"
        if RE_NECEKAN_FYZICKY.search(body): return "necekan_fyzicky"
        if RE_CALLBACK.search(body) or RE_MESSAGING.search(body): return "zavolejte_jindy"
        if NEDOV_RE.search(body): return "nedovolano"
        return "ostatni"


    now_utc = datetime.now(timezone.utc)
    # ---- STALE INPUT GUARD: zadny sf_*.json vstup nesmi byt starsi nez 30 min ----
    # Chrani pred situaci, kdy refresh bumpne generated_at, ale build bezi na STARYCH
    # sf_*.json (reuse misto cerstveho pullu) -> tise stale buffer / nedovolano / status.
    import time as _time
    _STALE_MAX = 30 * 60
    # POZOR: kontroluj jen soubory, ktere build SKUTECNE cte (ne vsechny sf_*.json v /tmp —
    # tam muzou byt leftover soubory od jinych behu, ktere by zpusobily false-positive).
    _CHECK_FILES = [
        "sf_status.json", "sf_status_all.json", "sf_ip.json", "sf_closed_status.json",
        "sf_feeds.json", "sf_incidents.json", "sf_heatmap.json", "sf_agents.json",
        "sf_agents_per_case.json", "sf_hourly_added.json", "sf_hourly_vin.json",
        "sf_hourly_rej.json", "sf_aws_open.json", "sf_aws_age.json", "sf_aws_other.json",
        "sf_phase2.json", "sf_audit_order_expected.json",
        "sf_cebia_audit_order_expected.json", "sf_prep_to_done.json",
        "sf_agent_activity_move.json", "sf_agent_activity_feed.json",
    ]
    _stale = []
    for _fn in _CHECK_FILES:
        _fp = os.path.join(TMP, _fn)
        try:
            _age = _time.time() - os.path.getmtime(_fp)
        except OSError:
            continue  # chybi -> resi degrade/regression guard, ne tady
        if _age > _STALE_MAX:
            _stale.append((_fn, int(_age / 60)))
    if _stale:
        raise SystemExit(
            "STALE INPUT GUARD: tyto SF vstupy jsou starsi nez 30 min (reuse starych dat "
            "misto cerstveho pullu): " + ", ".join(f"{n}({m}min)" for n, m in _stale) +
            ". Refresh MUSI znovu spustit prislusne SOQL. Nezapisuji /tmp/data.json."
        )
    nedov_data = []
    nedov_count = 0
    cc_recs = [r for r in ip_recs if r["Status"] == "Car check"]
    for c in cc_recs:
        cid = c["Id"]
        # Vsechny feedy case, newest first. Q4 vraci jen Type=TextPost (operatorske posty),
        # navic explicitne vyloucime systemove zaznamy "Case status updated"/"Case Created"
        # (Josef 2026-06-17) a feedy bez tela.
        cf_all = sorted(feeds_by_case.get(cid, []), key=lambda x: x.get("CreatedDate") or "", reverse=True)
        cf = [f for f in cf_all if f.get("Body") and not RE_SYSTEM_FEED.search(f["Body"])]
        nf = [f for f in cf if NEDOV_RE.search(f["Body"])]
        # Legacy kategorizovane feedy (4-kat) — drzime kvuli nCnt a zpetne kompatibilite
        cat_feeds = []
        for f in cf:
            cat = categorize(f.get("Body"))
            if cat:
                cat_feeds.append((cat, f))
        # b9 "kontakt" = feed, ze ktereho plyne pokus o kontakt = jakakoli z 6 kategorii
        # KROME 'ostatni' (interni @mention, FC objednani, dokumenty nejsou kontakt).
        contact_feeds = [f for f in cf if categorize_full(f.get("Body")) != "ostatni"]
        is_n = len(nf) > 0
        if is_n:
            nedov_count += 1
        ccd = parse_dt(c.get("CA_Car_Check_Date__c"))
        age_h = round((now_utc - ccd).total_seconds() / 3600) if ccd else None
        # b5 Prac. hod. — POUZE Po-Pa 08-17 (lokalne pro tuto tabulku; Josef 2026-06-17)
        age_w = working_hours_mf(ccd, now_utc) if ccd else None
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
        # b9 "Na uzavreni" (Josef 2026-06-17): VSECHNY podminky soucasne ->
        #   1) >= 3 kontaktni feedy (pokus o kontakt)
        #   2) mezi prvnim a poslednim kontaktem >= 2 prac. hodiny (Po-Pa 08-17)
        #   3) ZADNY blokacni incident u case
        pot_contacts = len(contact_feeds)
        pot_close = False
        pot_span_h = None
        if pot_contacts >= 3 and not case_incs:
            cts = sorted(parse_dt(f["CreatedDate"]) for f in contact_feeds)
            pot_span_h = working_hours_mf(cts[0], cts[-1])
            if pot_span_h >= 2:
                pot_close = True
        # b6 Posledni pokus + b7 Operator (Josef 2026-06-17): posledni REALNY feed post
        # (jakykoli typ, krome systemovych Case status updated/Created) a jeho autor.
        last_type = cat_feeds[0][0] if cat_feeds else None  # ponechano pro zpetnou kompat.
        last_d = None
        last_by = None
        if cf:
            last_d = cf[0]["CreatedDate"]
            last_by = (cf[0].get("CreatedBy") or {}).get("Name")
        # b7 Typ kontaktu — 6-kat label z POSLEDNIHO feedu (cf[0]); zadny feed -> None ("—")
        last_category = categorize_full(cf[0]["Body"]) if cf else None
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
            "potContacts": pot_contacts,
            "lastType": last_type,
            "lastCategory": last_category,
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

    # ---- Heatmap: CarAudit -> Audit Result (rolling 30 days) — optional (work-window only) ----
    matrix_result = None
    hm_result_total = 0
    try:
        with open(os.path.join(TMP, "sf_heatmap_result.json"), "r", encoding="utf-8") as _fhr:
            _hr_recs = json.load(_fhr).get("records", [])
        matrix_result = [[0] * 24 for _ in range(7)]
        for _r in _hr_recs:
            _idx = (int(_r["dow"]) + 5) % 7
            matrix_result[_idx][int(_r["hr"])] = int(_r["cnt"])
            hm_result_total += int(_r["cnt"])
    except FileNotFoundError:
        matrix_result = None

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

        # --- STALE-GUARD: po 12:00 nesmi byt 0 prichodu od 9:00 (skoro jiste nenactene Q9) ---
        _post9_in = sum(in_h.get(h, 0) for h in range(9, cur_hr + 1))
        if cur_hr >= 12 and _post9_in == 0:
            print("WARN: buffer_hourly vypada STALE (0 prichodu CA od 9:00 pres cely den) "
                  "-> Q9/Q10/Q11 zrejme nebezely cerstve. Vynechavam buffer_hourly.")
            buffer_hourly = None

    # ---- Capacity recommendation (CA prediction) — optional (Q_TP_VIN/Q_TP_REJ) ----
    def _load_tp(name):
        try:
            with open(os.path.join(TMP, name), "r", encoding="utf-8") as fh:
                return json.load(fh).get("records", [])
        except FileNotFoundError:
            return None
    tp_vin = _load_tp("sf_tp_vin.json")
    tp_rej = _load_tp("sf_tp_rej.json")
    capacity_reco = compute_capacity_reco(tp_vin, tp_rej, buffer_hourly, now_utc)

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

    # ---- AwS age buckets (Q17) — stari v Awaiting Selection k dnesku ----
    def _load_aws_age():
        try:
            with open(os.path.join(TMP, "sf_aws_age.json"), "r", encoding="utf-8") as fh:
                return json.load(fh).get("records", [])
        except FileNotFoundError:
            return None
    aws_age_recs = _load_aws_age()
    aws_age = None
    if aws_age_recs is not None:
        buckets = [0, 0, 0, 0]
        cases = []
        for r in aws_age_recs:
            dt = parse_dt(r.get("CA_Awaiting_Selection_Date__c"))
            if dt is None:
                continue
            wd = round(working_hours_between(dt, now_utc) / 8.0, 1)
            bi = 0 if wd < 2 else (1 if wd < 5 else (2 if wd < 10 else 3))
            buckets[bi] += 1
            ca = r.get("CarAudit__r") or {}
            cases.append({
                "id": r["Id"], "cn": r.get("CaseNumber"),
                "country": ca.get("Vendor_Country__c") or "N/A",
                "pref": bool((r.get("Order__r") or {}).get("Preferred__c")),
                "since": r.get("CA_Awaiting_Selection_Date__c"), "wd": wd,
            })
        cases.sort(key=lambda x: -x["wd"])
        aws_age = {"buckets": buckets, "total": sum(buckets), "cases": cases}

    # ---- AwS detailed breakdown a-f (Q17 enriched + Q18 other-cases) -> #awsDetail ----
    # Reuses Q17 records (must include Order__r.Status, CarAudit_Status__c, AccountId).
    # (f) reads sf_aws_other.json (Q18). Degrades gracefully if inputs missing.
    aws_detail = None
    if aws_age_recs is not None:
        EXC_AWS = {"APPROVED Awaiting Selection", "REJECT Awaiting Selection"}
        DONE_S, CLOSED_S = "CarAudit Done", "CarAudit Closed"
        ORDER_TERM = {"ord-lost", "ord-completed"}
        ORDER_ACCEPTED = {"ord-caraudit-accepted", "ord-contract-data-collected",
                          "ord-contract-uploaded", "ord-contract-accepted", "ord-contract-paid",
                          "ord-import", "ord-service", "ord-delivery", "ord-completed"}
        def _ostat(r):
            o = r.get("Order__r"); return (o or {}).get("Status") if o else None
        base = [r for r in aws_age_recs if r.get("CarAudit_Status__c") not in EXC_AWS]
        have_order = any(_ostat(r) for r in base)
        BL = ["le2", "b25", "b510", "gt10"]
        bk = {k: {"active": [], "inactive": []} for k in BL}
        a_ids = set(r["Id"] for r in base)
        for r in base:
            dt = parse_dt(r.get("CA_Awaiting_Selection_Date__c"))
            if dt is None:
                continue
            wd = round(working_hours_between(dt, now_utc) / 8.0, 1)
            bi = BL[0] if wd < 2 else (BL[1] if wd < 5 else (BL[2] if wd < 10 else BL[3]))
            ost = _ostat(r)
            ca = r.get("CarAudit__r") or {}
            item = {"id": r["Id"], "cn": r.get("CaseNumber"),
                    "country": ca.get("Vendor_Country__c") or "N/A",
                    "pref": bool((r.get("Order__r") or {}).get("Preferred__c")),
                    "wd": wd, "order": ost}
            active = ost not in ORDER_TERM
            bk[bi]["active" if active else "inactive"].append(item)
        aws_dtl_total = len(base)
        active_n = sum(len(bk[k]["active"]) for k in BL)
        def _load_other():
            try:
                with open(os.path.join(TMP, "sf_aws_other.json"), "r", encoding="utf-8") as fh:
                    return json.load(fh).get("records", [])
            except FileNotFoundError:
                return None
        other_recs = _load_other()
        f_available = other_recs is not None
        f_inprog, f_wait = [], []
        if f_available:
            from collections import defaultdict as _dd2
            byacc = _dd2(list)
            for r in other_recs:
                byacc[r.get("AccountId")].append(r)
            aws_by_acct = _dd2(list)
            for r in base:
                aws_by_acct[r.get("AccountId")].append(r)
            for acc in set(r.get("AccountId") for r in base if r.get("AccountId")):
                q = []
                for r in byacc.get(acc, []):
                    if r["Id"] in a_ids:
                        continue
                    s = r.get("Status"); o = _ostat(r)
                    if s == CLOSED_S:
                        continue
                    if s == DONE_S:
                        if o in ORDER_ACCEPTED or o == "ord-lost":
                            continue
                        kind = "wait"
                    else:
                        kind = "inprog"
                    q.append({"cn": r.get("CaseNumber"), "id": r["Id"], "status": s, "order": o, "kind": kind})
                if q:
                    awscases = []
                    for x in aws_by_acct.get(acc, []):
                        dtx = parse_dt(x.get("CA_Awaiting_Selection_Date__c"))
                        awscases.append({"cn": x.get("CaseNumber"), "id": x["Id"],
                                         "wd": round(working_hours_between(dtx, now_utc) / 8.0, 1) if dtx else None})
                    entry = {"aws": awscases, "others": q}
                    (f_inprog if any(o["kind"] == "inprog" for o in q) else f_wait).append(entry)
        aws_detail = {
            "total": aws_dtl_total, "active": active_n, "inactive": aws_dtl_total - active_n,
            "have_order": have_order, "f_available": f_available,
            "f_customers_total": len(set(r.get("AccountId") for r in base if r.get("AccountId"))),
            "buckets": {k: {"active": bk[k]["active"], "inactive": bk[k]["inactive"]} for k in BL},
            "f_inprog": f_inprog, "f_wait": f_wait,
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
    # Akumulátor pattern — dny v okně posledního Q16 fetche (~7 dní) se přepisují per-op MAXem
    # (oprava neúplných same-day běhů), dny mimo okno zůstávají immutable. Display window 44 prac. dnů.
    # Display window: 44 pracovních dnů (~2 měsíce zpět).
    # History file `data/prep_to_done_history.json` se commituje vedle data.json.
    HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "prep_to_done_history.json")
    history = {"carvago": {}, "cebia": {}}
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH, "r", encoding="utf-8") as fh:
                history = json.load(fh)
                history.setdefault("carvago", {})
                history.setdefault("cebia", {})
        except Exception as e:
            print(f"  WARN: failed to load prep_to_done_history.json: {e}", file=sys.stderr)

    if ptd_recs is not None:
        from datetime import date as _date
        DOW_CS = ["po", "út", "st", "čt", "pá", "so", "ne"]
        today_pg = datetime.now(PRAGUE).date()
        today_key = today_pg.isoformat()
        # Build fresh per-day per-user counts from current Q16 fetch
        fresh = {"carvago": {}, "cebia": {}}
        for r in ptd_recs:
            rt = r.get("RecordType")
            if rt == "CarAudit":
                sec = "carvago"
            elif rt == "Cebia CarAudit":
                sec = "cebia"
            else:
                continue
            name = r.get("CreatedByName") or "Unknown"
            cd = parse_dt(r.get("CreatedDate"))
            if cd is None:
                continue
            cd_local = cd.astimezone(PRAGUE)
            if cd_local.weekday() >= 5:  # skip weekends
                continue
            day_key = cd_local.date().isoformat()
            day_map = fresh[sec].setdefault(day_key, {})
            day_map[name] = day_map.get(name, 0) + 1
        # Merge into history (oprava 2026-06-24 — viz "stale 23.6" bug):
        #  - dnešek (today_key): vždy přepiš živou (kumulativní) hodnotou
        #  - ostatní dny přítomné v aktuálním Q16 fetch (okno LAST_N_DAYS:7): per-operátor MAX.
        #    Tím se den OPRAVÍ na svůj konečný počet i když poslední same-day běh proběhl
        #    jen v půlce dne (počty během dne jen rostou). MAX zároveň nikdy nesníží už
        #    zaznamenaný počet kvůli transient under-fetch / SF LIMIT truncaci.
        #  - dny mimo fetch okno (8-44 dní zpět): nejsou ve `fresh`, zůstávají immutable.
        for sec in ("carvago", "cebia"):
            for day_key, by_user in fresh[sec].items():
                if day_key == today_key:
                    history[sec][day_key] = by_user
                else:
                    merged = dict(history[sec].get(day_key, {}))
                    for name, cnt in by_user.items():
                        if cnt > merged.get(name, 0):
                            merged[name] = cnt
                    history[sec][day_key] = merged
        # Save updated history back to file (will be commited alongside data.json)
        os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
        with open(HISTORY_PATH, "w", encoding="utf-8") as fh:
            json.dump(history, fh, ensure_ascii=False, indent=1, sort_keys=True)
    # Build display from history (regardless of whether Q16 ran — show stale history if Q16 missing)
    if history["carvago"] or history["cebia"]:
        from datetime import date as _date
        DOW_CS = ["po", "út", "st", "čt", "pá", "so", "ne"]
        today_pg = datetime.now(PRAGUE).date()
        DAYS_WINDOW = 44  # working days ≈ 2 months
        days_list = []
        cur = today_pg
        while len(days_list) < DAYS_WINDOW:
            if cur.weekday() < 5:
                days_list.append(cur)
            cur = cur - timedelta(days=1)
        date_keys = [d.isoformat() for d in days_list]
        def _section_from_history(sec_name):
            sec_data = history.get(sec_name, {})
            # Gather all operators that appear in any day in window
            ops = set()
            for dk in date_keys:
                ops.update(sec_data.get(dk, {}).keys())
            users = []
            for name in ops:
                row = {"name": name, "by_date": {}, "total": 0}
                for dk in date_keys:
                    cnt = sec_data.get(dk, {}).get(name, 0)
                    row["by_date"][dk] = cnt
                    row["total"] += cnt
                if row["total"] > 0:
                    users.append(row)
            users.sort(key=lambda u: u["total"], reverse=True)
            day_totals = [sum(sec_data.get(dk, {}).values()) for dk in date_keys]
            max_cell = max([max(sec_data.get(dk, {}).values()) for dk in date_keys if sec_data.get(dk)] + [0])
            return {"users": users, "day_totals": day_totals, "max_cell": max_cell}
        prep_to_done_daily = {
            "today_iso": today_pg.isoformat(),
            "days": [{"date": d.isoformat(), "label": f"{d.day}.{d.month}.", "dow": DOW_CS[d.weekday()]} for d in days_list],
            "carvago": _section_from_history("carvago"),
            "cebia": _section_from_history("cebia"),
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
    # NOVÝ INVARIANT (2026-06-10): total == closed + phase2 + ip_total (jinak procenta jdou přes 100%)
    if total != (closed + phase2 + ip_total):
        raise SystemExit(
            f"\n\n🛑 TIER1 INVARIANT FAILED: total={total} != closed({closed})+phase2({phase2})+ip({ip_total})={closed+phase2+ip_total}.\n"
            f"Probable cause: Q1 vrátil nekonzistentní hodnoty (chybějící Status v GROUP BY nebo space-only Status).\n"
            f"Toto je root cause '426% closed rate' bugu (2026-06-10). NEPUSHUJI.\n"
        )
    # NOVÝ INVARIANT: žádné procento přes 100% (closed/total a phase2/total)
    if total > 0:
        if closed / total > 1.0 or phase2 / total > 1.0:
            raise SystemExit(
                f"\n\n🛑 TIER1 RATIO FAILED: closed/total={closed/total:.2%} phase2/total={phase2/total:.2%} (oba musí být <= 100%).\n"
                f"Pravděpodobně total spočítaný ze špatné podmnožiny. NEPUSHUJI.\n"
            )
    if ip_total > 300:
        raise SystemExit(f"\n\n🛑 SANITY CHECK FAILED: ip_total={ip_total} (max 300 expected). Filtr chybí v Q1.\n")
    if closed > 3000:
        raise SystemExit(f"\n\n🛑 SANITY CHECK FAILED: closed={closed} (max 3000 expected/month). Filtr chybí v Q1.\n")

    # ---- Agent activity dnes (po hodinach) ----
    # Ctu dva agregovane SOQL vystupy. Pokud chybi oba -> sekce se vynecha (degrade),
    # NENI v CRITICAL_KEYS, takze jeji absence nikdy nezablokuje refresh.
    try:
        _aa_move = load("sf_agent_activity_move.json").get("records", [])
    except Exception:
        _aa_move = None
    try:
        _aa_feed = load("sf_agent_activity_feed.json").get("records", [])
    except Exception:
        _aa_feed = None
    agent_activity = None
    if _aa_move is not None or _aa_feed is not None:
        agent_activity = compute_agent_activity(_aa_move or [], _aa_feed or [], now_utc)

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
    if matrix_result is not None:
        output["heatmap_result"] = {
            "matrix": matrix_result,
            "days_per_dow": days_per_dow,
            "total_cases": hm_result_total,
            "start_date": start_pg.strftime("%Y-%m-%d"),
            "end_date": today_pg.strftime("%Y-%m-%d"),
        }
    if buffer_hourly is not None:
        output["buffer_hourly"] = buffer_hourly
    if capacity_reco is not None:
        output["capacity_reco"] = capacity_reco
    if aws_split is not None:
        output["aws_split"] = aws_split
    if aws_age is not None:
        output["aws_age"] = aws_age
    if aws_detail is not None:
        output["aws_detail"] = aws_detail
    if phase2_tables is not None:
        output["phase2_tables"] = phase2_tables
    if audit_order_expected is not None:
        output["audit_order_expected"] = audit_order_expected
    if cebia_audit_order_expected is not None:
        output["cebia_audit_order_expected"] = cebia_audit_order_expected
    if prep_to_done_daily is not None:
        output["prep_to_done_daily"] = prep_to_done_daily
    if agent_activity is not None:
        output["agent_activity"] = agent_activity
    # ---- REGRESSION GUARD: nedovol, aby z data.json tiše zmizela sekce, kterou jsme uz meli ----
    # Porovna novy output s naposledy commitnutym ./data.json. Pokud kriticka sekce
    # (vcetne aws_detail) byla driv pritomna a ted chybi -> FAIL, nezapisuj a nepushuj.
    CRITICAL_KEYS = ["tier1", "status_breakdown", "aws_age", "aws_detail",
                     "country_breakdown", "agent_breakdown",
                     "cebia_audit_order_expected", "prep_to_done_daily"]
    try:
        _prev_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
        with open(_prev_path, "r", encoding="utf-8") as _pf:
            _prev = json.load(_pf)
    except Exception:
        _prev = {}
    _lost = [k for k in CRITICAL_KEYS
             if _prev.get(k) is not None and output.get(k) is None]
    if _lost:
        raise SystemExit(
            "REGRESSION GUARD: tyto sekce existovaly v predchozim data.json, ale "
            "v novem chybi: " + ", ".join(_lost) + ". Nezapisuji /tmp/data.json. "
            "Dobehni chybejici SOQL (napr. Q17 sf_aws_age + Q18 sf_aws_other pro aws_detail) a spust build znovu."
        )
    with open(os.environ.get("FCM_OUT","/tmp/data.json"), "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    print(
        f"total={total} ip={ip_total} closed={closed} "
        f"cc={len(cc_recs)} nedov={nedov_count} heatmap={hm_total}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
