#!/usr/bin/env python3
"""Builds backlog_estimate.json for the 1stCall Monitor 'Dohad zustatku' section.
Reads QB query responses saved as /tmp/be/sf_be_*.json (Salesforce REST format).
Carvago capacity from CA_CarAudit_Done_Date per Owner (LAST_N_DAYS:14).
Cebia capacity = None (Done date not populated -> measure via CaseHistory Q16)."""
import json, os, sys, math, datetime

BE_DIR = os.environ.get("BE_DIR", "/tmp/be")
OUT    = sys.argv[1] if len(sys.argv) > 1 else "backlog_estimate.json"
WD_CAP = 11            # working days in LAST_N_DAYS:14 window (incl. today)
AVG_PERSON = 5.6
TARGET = 5
NAMED = {"Marek Moťovský","Daniel Forman","Daniel Noga","Vladimír Uher","Martin Linhart"}

def load(name):
    p = os.path.join(BE_DIR, name)
    if not os.path.exists(p): return None
    with open(p, encoding="utf-8") as f: return json.load(f)

def count(name):
    d = load(name)
    return int(d.get("totalSize", 0)) if d else 0

def grp_sum(name, key="c"):
    d = load(name); 
    if not d: return 0
    return sum(int(r.get(key, 0)) for r in d.get("records", []))

def variant(tag, with_cap):
    backlog_now = grp_sum(f"sf_be_backlog_{tag}.json")
    arrived = count(f"sf_be_arrived_{tag}.json")
    done    = count(f"sf_be_donetoday_{tag}.json")
    start   = backlog_now - arrived + done
    promised= count(f"sf_be_promised_{tag}.json")
    prom_w  = count(f"sf_be_noshow_prom_{tag}.json")
    deliv_w = count(f"sf_be_noshow_deliv_{tag}.json")
    realization = round(deliv_w/prom_w, 4) if prom_w else None
    inflow  = round(promised*realization, 1) if realization is not None else None
    out = {
        "backlog_now": backlog_now, "arrived_today": arrived, "done_today": done,
        "start_of_day": start, "promised_today": promised,
        "realization": realization, "no_show": round(1-realization,4) if realization is not None else None,
        "inflow": inflow,
    }
    if with_cap:
        cap = load(f"sf_be_cap_{tag}.json")
        by_owner = {}; named=0.0; other=0.0
        if cap:
            for r in cap.get("records", []):
                nm = r.get("n") or "—"; per = round(int(r.get("c",0))/WD_CAP, 1)
                label = nm if nm in NAMED else "Ostatní / systém"
                by_owner[label] = round(by_owner.get(label,0.0)+per, 1)
            named = round(sum(int(r["c"]) for r in cap["records"] if r.get("n") in NAMED)/WD_CAP,1)
            other = round(sum(int(r["c"]) for r in cap["records"] if r.get("n") not in NAMED)/WD_CAP,1)
        capacity = round(named+other,1)
        end = max(0, round(start + (inflow or 0) - capacity, 1))
        required = start + (inflow or 0) - TARGET
        extra_ca = max(0, round(required - capacity, 1))
        out.update({
            "capacity": capacity, "capacity_named": named, "capacity_other": other,
            "capacity_by_owner": by_owner, "end_of_day": end,
            "extra_ca_for_target": extra_ca,
            "extra_people_for_target": math.ceil(extra_ca/AVG_PERSON) if extra_ca>0 else 0,
        })
    else:
        out.update({"capacity": None, "capacity_source": "casehistory_prep_done",
                    "end_of_day": None,
                    "note": "Cebia Done date not populated; capacity via CaseHistory Prep->Done (Q16)"})
    # pipeline buckets
    pipe = load(f"sf_be_pipeline_{tag}.json")
    if pipe:
        today = datetime.date.today(); od=tod=tom=0
        for r in pipe.get("records", []):
            ca = r.get("CarAudit__r") or {}
            dt = ca.get("Promised_Delivery_Date__c")
            if not dt: continue
            d = datetime.date.fromisoformat(dt[:10])
            if d < today: od+=1
            elif d == today: tod+=1
            elif d == today+datetime.timedelta(days=1): tom+=1
        out["pipeline"] = {"overdue":od, "today":tod, "tomorrow":tom}
    return out

now = datetime.datetime.now()
res = {
    "generated_at_local": now.strftime("%Y-%m-%dT%H:%M:%S"),
    "target": TARGET, "window_capacity_wd": WD_CAP, "window_noshow_days": 32,
    "carvago": variant("cvg", True),
    "cebia":   variant("cebia", False),
}
with open(OUT, "w", encoding="utf-8") as f: json.dump(res, f, ensure_ascii=False, indent=2)
print("WROTE", OUT); print(json.dumps(res, ensure_ascii=False, indent=2))
