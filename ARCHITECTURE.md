# 1stCall Monitor — Architecture & Invariants

**Aktualizováno**: 2026-06-10
**Účel tohoto dokumentu**: zachytit kritické invarianty dashboardu, které **nesmí být změněny** bez explicitního požadavku. Pokud agent (Claude) aktualizuje data, musí dodržet všechny pravidla zde — jinak hrozí regrese.

## Repo struktura

```
firstcall-monitor/
├── index.html              — statický dashboard (React + Babel-in-browser)
├── data.json               — generovaná data (commit-uje refresh job)
├── build_data.py           — Python build script (čte sf_*.json z /tmp, píše data.json)
├── data/
│   └── prep_to_done_history.json  — IMMUTABLE historický akumulátor (commit-uje se vedle data.json)
└── refresh.py              — alternativa s simple_salesforce (NOT used)
```

## Datový tok

```
SF (SOQL přes MCP)
   ↓ 18 dotazů
/tmp/sf_*.json
   ↓ build_data.py
data.json + data/prep_to_done_history.json (akumulátor)
   ↓ git push
GitHub Pages → live dashboard (auto-refresh 5min)
```

## Kritické invarianty (NEPORUŠOVAT)

### 1. PREP heatmap whitelist filter (index.html)

```js
const PDC_ALLOWED_OPERATORS = new Set([
  'Marek Moťovský', 'Daniel Forman', 'Daniel Noga',
  'Vladimír Uher', 'Martin Linhart',
]);
```

V `renderPrepToDoneHeatmap()`. Filtrují se **oba** sekce (Carvago + Cebia). Jakákoli změna whitelistu = business rozhodnutí, ne tech.

### 2. PREP historický akumulátor

- Soubor: `data/prep_to_done_history.json`
- Schema: `{"carvago": {"YYYY-MM-DD": {"Operator Name": count}}, "cebia": {...}}`
- **Immutability rule** v build_data.py:
  - **Dnešní den** (Prague tz): vždy přepsán z čerstvého Q16
  - **Včerejšek a starší**: zapsáno jen pokud klíč chybí — JINAK NETKNUT
- Akumulátor roste sám každým refreshem. Po roce ~100 KB, zanedbatelné.
- Window pro display: `DAYS_WINDOW = 44` working days (~2 měsíce). Edit v build_data.py.
- **Nikdy** přímo nepřepisovat `data/prep_to_done_history.json` ze single SOQL fetch — destroyed history je nenávratná.

### 3. PREP medaile = AKTUÁLNÍ TÝDEN

Render funkce v index.html sortuje filtered users podle `_weekTotal` (sum of by_date Pondělí → dnes ISO-week). 🥇🥈🥉 jde top 3 dle tohoto pořadí.

Tiebreaker = 44-day total (pro stabilní pořadí v Pondělí ráno před prvními transitions).

### 4. Header bez countdown

Element `#countdown`, funkce `tickCountdown`/`resetCountdown`, var `nextRefreshAt`, CSS class `.countdown` byly úmyslně odstraněny. Uživatel je považoval za zavádějící.

Auto-refresh stále funguje (`setInterval(loadData, REFRESH_MS)`), jen není viditelný countdown.

### 5. Phase 2 tables

`data.json.phase2_tables` schema:
```json
{
  "statuses": ["Auditor selection","Audit order","Audit result","CarAudit preparation"],
  "preferred": {<status>: {"pref": N, "nopref": M}},
  "age": {<status>: {<bucket>: {"pref": N, "nopref": M}}},
  "buckets": ["lt2","b23","b35","b57","gt7"],
  "bucket_labels": {"lt2": "< 2 prac. dnů", ...}
}
```

Source: Q13 (records s Status IN P2_STATUSES + CA_Auditor_Selection_Date__c + Order__r.Preferred__c).
Age bucket = `working_hours_between(aws_dt, now) / 8.0` (working days).

### 6. Nedovoláno = VŠECHEN open Car check

`len(data.json.nedovolano)` MUSÍ být == `data.json.status_breakdown["Car check"]`. Pokud ne, Q2 běžel s THIS_MONTH filtrem (špatně) — chce **all-open**.

### 7. Skill orchestrace

Scheduled task `firstcall-monitor-refresh` (hourly Mon-Fri 08-19 UTC) volá `build_data.py` po sběru ~18 SOQL dotazů. Self-check 1+2+3 po pushi ověřuje, že na `origin/main` skutečně landla čerstvá data.

V `Build & push` sekci skillu: `git add data.json data/` — **nezapomenout commit-ovat history file** vedle data.json.



### 8. lastCategory v nedovolano detail (2026-06-16)

`data.json.nedovolano[i].lastCategory` — 6-kategorie posledního CaseFeed záznamu pro každý otevřený Car Check case:

- **prodano** — vůz prodán / není k dispozici / bez zájmu (verkauft, no interest, gutachten verweigert)
- **rezervovano** — rezervováno pro jiného zákazníka (reserviert für, immer noch reserviert)
- **necekan_fyzicky** — vůz fyzicky chybí (noch nicht angeliefert, im transport)
- **zavolejte_jindy** — call back / později / slíbil zaslat info (spaeter anrufen, fin sendet per mail, ozve se)
- **nedovolano** — nedostupný (nedovoláno, nicht erreicht, mailbox, im Kundengespräch, na dovolené)
- **ostatni** — bez pattern matche (interní @mentions, FC objednání, dokumenty, případy bez feedu)

Priority: terminal stavy (prodano, rezervovano) → necekan_fyzicky → akce (zavolejte_jindy) → status (nedovolano) → ostatni.

build_data.py: `categorize_full()` funkce, používá `RE_PRODANO`, `RE_REZERVACE`, `RE_NECEKAN_FYZICKY`, `RE_CALLBACK`/`RE_MESSAGING`, `NEDOV_RE`. Vstup = `cf[0].Body` (poslední feed po DESC sortu CreatedDate).

index.html `renderNedovTable`: render přes `cat = d.lastCategory || legacy lastType mapping`. Každá kategorie má barevný badge s tooltip.

Sort priorita: nedovolano > rezervovano > prodano > zavolejte_jindy > necekan_fyzicky > ostatni (highest = nejakutnější).

## Anti-patterns

Viz `firstcall-monitor-refresh/SKILL.md` sekce "ANTI-PATTERNS".

## Commit history klíčových změn

- `4cda2c7` (2026-06-10): remove zavádějící countdown
- `a32958e` (2026-06-10): medaile podle aktuálního týdne
- `b20ff49` (2026-06-10): backfill 44 dnů historie
- `23d30d4` (2026-06-10): historický akumulátor (data/prep_to_done_history.json)
- `48be20a` (2026-06-10): PDC whitelist filter 5 operátorů
