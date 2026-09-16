# sql/ — модуль M8

База `home4331` (PostgreSQL 18): `01_create_load.sql` — DDL (COPY робить
`src/load_db.py`), далі `02_validation` (якість даних), `03_outages`
(gaps-and-islands), `04_climate` (кімнати×місяці×години), `05_cooking`
(віконна рамка), `06_marts` (вітрини-`VIEW`). Звірка з Python — у
`results/m8_sql_check.md`.

## Запуск проти реального сервера
```powershell
$env:PGHOST="localhost"; $env:PGPORT="5432"; $env:PGUSER="postgres"
$env:PGPASSWORD="..."   # 🔧 крок для Даші/Влада — пароль знає лише власник
$env:PGDATABASE="home4331"; $env:PYTHONUTF8="1"

python -m src.load_db                     # створює home4331, вантажить 5 таблиць
psql -d home4331 -f sql/02_validation.sql
psql -d home4331 -f sql/03_outages.sql
psql -d home4331 -f sql/04_climate.sql
psql -d home4331 -f sql/05_cooking.sql
psql -d home4331 -f sql/06_marts.sql
```
З'єднання — лише зі змінних `PG*` (без пароля в коді); повторний запуск
`load_db.py` безпечний (`DROP TABLE IF EXISTS ... CASCADE` перед створенням).
