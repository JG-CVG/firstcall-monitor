# 1st Call Monitor

Statický dashboard pro 1st Call (CarAudit) sledování v Carvago. Data se aktualizují každých 10 minut z Salesforce přes GitHub Actions a publikují na GitHub Pages (chráněné Cloudflare Access).

## Co dashboard zobrazuje

- Tier 1/2/3 KPI: Total, Closed, Phase 2+, In Progress (New CA, DV, Car Check, VIN Check, Awaiting Selection)
- Car Check — nedovoláno detail (per case)
- Closed reasons (Phase 1) seřazené dle funnel pořadí
- Vendor Country přehled
- CarAudit New Heatmap (24h × den týdne, plovoucí měsíc)
- CarAudit New Heatmap (pracovní okno Po-Pá 8-17, So 8-13, s přerozdělením mimo-pracovních case)

## Architektura

```
GitHub Actions cron */10 min
  └─ refresh.py
      └─ Salesforce SOQL (service account)
          └─ data.json (commit + push)

GitHub Pages
  └─ index.html (fetch ./data.json)

Cloudflare Access
  └─ Gate na carvago.com email
```

## Setup — krok po kroku

### 1. Vytvoř Salesforce service user

V Salesforce setup:

1. **Setup → Users → New User**
   - Username: `dashboard@carvago.com.prod` (nebo podobné)
   - Email: aliased na sdílenou inbox (např. `it@carvago.com`)
   - Profile: **Read-Only** (custom profile s API access enabled, ale bez UI permissions)
   - License: Salesforce Platform (nejlevnější s API)
2. **Reset Security Token**: po prvním přihlášení jako tento user, Settings → My Personal Information → Reset My Security Token. Token přijde mailem.
3. **Object access**: user musí mít read access na:
   - Case (vč. polí: Status, CaseNumber, CA_New_CarAudit_Date__c, CA_Car_Check_Date__c, RecordType, CarAudit_Status__c, atd.)
   - CarAudit__r (Car_Audit__c objekt — vč. Vendor_Country__c)
   - Order (pole Instamotion_Customer__c)
   - CaseFeed (read)
   - Incident__c (read — pole Case__c, Subject__c, Estimated_Resolution_Date__c, Type__c, Status__c)
4. Ověř loginem manuálně v Salesforce, že vidíš pár CarAudit cases.

### 2. Vytvoř GitHub repo

```bash
# Na GitHub vytvoř nové repo: JG-CVG/firstcall-monitor (private)
# Clone lokálně
git clone git@github.com:JG-CVG/firstcall-monitor.git
cd firstcall-monitor

# Nakopíruj všechny soubory z tohoto adresáře (refresh.py, index.html, requirements.txt,
# .github/, .gitignore, README.md) do repa
# Commit:
git add .
git commit -m "Initial dashboard scaffold"
git push origin main
```

### 3. GitHub Secrets

V `JG-CVG/firstcall-monitor` repu: **Settings → Secrets and variables → Actions → New repository secret**

Přidej 3 secrety:
- `SF_USERNAME` = `dashboard@carvago.com.prod`
- `SF_PASSWORD` = heslo service usera
- `SF_TOKEN` = security token (z emailu po resetu)

Volitelně (Variables, ne Secrets):
- `SF_DOMAIN` = `login` (default, pro produkci) nebo `test` (pro sandbox)
- `SF_BASE_URL` = `https://carvago.lightning.force.com` (default)

### 4. První spuštění

V GitHub repo: **Actions → Refresh 1stCall data → Run workflow** (manuálně).

Pokud projde, uvidíš nový commit s `data.json`. Pokud selže, koukni do logu kroku „Refresh data" — typicky se jedná o:
- Špatný SF token (po resetu trvá pár minut, než zafunguje)
- Chybějící read access na nějaké pole — log napoví
- IP whitelist na SF straně (service user může vyžadovat trusted IP range; GitHub Actions IPs jsou veřejné — buď whitelist GitHub IPs, nebo nastav profil bez IP restrikcí)

### 5. Aktivuj GitHub Pages

**Settings → Pages**:
- Source: **Deploy from a branch**
- Branch: **main** / **/ (root)**
- Save

Po pár minutách bude dashboard dostupný na `https://jg-cvg.github.io/firstcall-monitor/`.

### 6. Cloudflare Access (gate)

**Pozor**: GitHub Pages je defaultně public. Bez Cloudflare Access by URL bylo přístupné komukoliv s odkazem (citlivá data!). Nastavení:

1. V Cloudflare dashboard přidej carvago.com (nebo subdoménu typu `firstcall.carvago.com`).
2. **DNS → CNAME**: `firstcall` → `jg-cvg.github.io` (proxied přes Cloudflare = oranžový mráček).
3. V `JG-CVG/firstcall-monitor` repo: **Settings → Pages → Custom domain** → `firstcall.carvago.com`. Tím GitHub Pages vystaví TLS cert.
4. **Cloudflare Zero Trust → Access → Applications → Add Application** (Self-hosted):
   - Application domain: `firstcall.carvago.com`
   - Session duration: 24h
   - Policy: **Allow** if Email domain is `carvago.com`
   - (Volitelně) další politika pro specifické emaily / Okta groups
5. Cloudflare automaticky vystaví login page. Uživatelé z `@carvago.com` se přihlásí přes Google/Microsoft SSO (záleží jak je Cloudflare Access nastaven).

Hotovo. Tým otevře `https://firstcall.carvago.com`, autentizuje se přes Carvago email, vidí dashboard. Data se obnovuje samo každých 10 min.

## Lokální development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Nastav env vars
export SF_USERNAME=dashboard@carvago.com.prod
export SF_PASSWORD=...
export SF_TOKEN=...

# Spusť refresh
python refresh.py
# Vygeneruje data.json

# Spusť lokální server pro index.html
python -m http.server 8000
# Otevři http://localhost:8000
```

## Monitoring & údržba

- **Failed runs**: GitHub Actions ti pošle email při selhání jobu. Můžeš nastavit i Slack notifikaci (action: `slackapi/slack-github-action`).
- **SF rate limits**: každý refresh spotřebuje ~5–7 API calls. 10 min cron × 144 spuštění za den × 7 calls = ~1 000 calls/den. SF org má typicky 100k+ calls/24h — daleko pod limitem.
- **SF token expiry**: SF security token se neresetuje sám, ale POKUD někdo změní heslo service usera, token expiruje. Doporučuji oddělený service account, jehož heslo nikdo neopravuje.
- **Schema změny v Salesforce**: pokud admin přidá/přejmenuje pole, `refresh.py` může spadnout. Test po každé velké SF release.
- **Dashboard UX**: pokud chceš změnu (např. přidat sloupec, změnit barvy, přeřadit sekce) — edituj `index.html` a pushni. Změna se objeví na další reload.

## Rozšíření do budoucna

- **Push notifikace na lebky** (case s potClose=true): refresh.py může na konci poslat Slack message do `#1stcall-monitor` kanálu, pokud najde nový case s lebkou.
- **Per-country dashboardy**: parametr `?country=DE` ve URL filtruje data v JS.
- **Historie**: archivovat staré `data.json` (např. denně) do `history/` složky pro trendy.
- **OAuth místo password**: Connected App + JWT bearer flow odstraní závislost na security tokenu (nevyprší při změně hesla).

## Soubory v repo

```
firstcall-monitor/
├── .github/
│   └── workflows/
│       └── refresh.yml      # GitHub Actions: cron */10 min
├── .gitignore
├── README.md                # tento soubor
├── data.json                # generuje refresh.py (na začátku není)
├── index.html               # statický dashboard
├── refresh.py               # Python script, dotazuje SF
└── requirements.txt         # simple-salesforce
```

<!-- deploy nudge 1779793393 -->

<!-- pages reset 1779793751 -->
