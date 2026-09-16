# M8 — звірка Python vs SQL

База `home4331` (PostgreSQL 18), завантажена `src/load_db.py` з `data/clean/*.csv`
і `results/m2_outages.csv`. Усі запити — у `sql/02_validation.sql` … `sql/06_marts.sql`.

| показник | Python (results/*.json) | SQL (`home4331`) | збіг? |
|---|---|---|---|
| рядків у readings | 53 946 (`m1_quality.json: hourly_rows`) | 53 946 (`SELECT COUNT(*) FROM readings`) | ✅ |
| покриття % Bedroom, температура | 98.2 (`coverage_bedroom_temp`) | 98.2 (`02_validation.sql`, п.4) | ✅ |
| кількість відключень | 5 (`m2_outages.json: n_events_total`) | 5 (`03_outages.sql`, gaps-and-islands) | ✅ |
| сумарні години відключень | 35.0 (`total_hours`) | 35 (`03_outages.sql`, п.5) | ✅ |
| uptime % | 99.205 (`uptime_pct`) | 99.205 (`03_outages.sql`, п.5 і `v_dashboard_kpis`) | ✅ |
| подій готовки (погодинне правило) | 26 (`m6_cooking.json: n_events_hourly`) | 26 (`05_cooking.sql`, п.3) | ✅ |
| середня температура Bedroom, березень 2026 | 22.67 °C (`m3_monthly.csv`, 2026-03) | 22.67 (`04_climate.sql`, п.1 / `v_monthly_climate`) | ✅ |
| комфорт % Bedroom (20–24 °C) | 42.3 (`bedroom_comfort_temp_ok_pct`) | 42.3 (`04_climate.sql`, п.5 / `v_dashboard_kpis`) | ✅ |

Усі 8 контрольних чисел збіглися точно — розбіжностей виправляти не довелось.

## Дві пастки, які довелось явно врахувати в SQL (щоб числа збіглися)

1. **Покриття % рахується від власного календаря датчика, не від спільного.**
   У Python (M1, `src/cleaning.py`) кожен сенсор ресемплиться від СВОЄЇ першої
   появи до спільного максимуму — пізніше встановлений датчик (Kitchen, NAS,
   сервер) не штрафується за місяці до монтажу. У `readings` цей "власний"
   календар уже "запечений": просто `COUNT(*)` по `entity_id` = його
   calendar_hours. Перша версія запиту (спільний `generate_series` для всіх
   сенсорів одразу) давала занижені % для пізніх датчиків (напр. Kitchen —
   60.9 замість 68.0) — виправлено.
2. **Рамка для детекції готовки — `ROWS BETWEEN 3 PRECEDING AND 1 PRECEDING`**,
   не `AND CURRENT ROW`, як у ілюстративному прикладі в специфікації M8.
   Означення взято з фактичного коду M6 (`src/cooking.py:
   detect_events_hourly`) — приріст рахується відносно середнього за
   3 ГОДИНИ ДО поточної, інакше поточний стрибок впливав би сам на себе.
   З цією рамкою SQL дає рівно 26 подій, як і Python.

## Команди для власника (реальний сервер, база `home4331`)

```powershell
$env:PGHOST = "localhost"
$env:PGPORT = "5432"
$env:PGUSER = "postgres"
$env:PGPASSWORD = "..."      # 🔧 крок для Даші/Влада — пароль знає лише власник
$env:PGDATABASE = "home4331"
$env:PYTHONUTF8 = "1"

python -m src.load_db
psql -d home4331 -f sql/02_validation.sql
psql -d home4331 -f sql/03_outages.sql
psql -d home4331 -f sql/04_climate.sql
psql -d home4331 -f sql/05_cooking.sql
psql -d home4331 -f sql/06_marts.sql
```
