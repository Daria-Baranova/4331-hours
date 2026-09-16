-- =============================================================================
-- 01_create_load.sql — модуль M8, проєкт «4331 година»
-- =============================================================================
-- Що робить: створює 5 таблиць бази home4331 (довідники rooms/sensors +
-- факти readings/weather/outages), первинні ключі, індекс по часу.
-- Нові техніки: DDL, типи (timestamptz, boolean), PRIMARY KEY, FOREIGN KEY,
-- CREATE INDEX.
--
-- Саме завантаження даних (COPY ... FROM STDIN) робить Python-скрипт
-- `src/load_db.py` через psycopg2 `copy_expert` — це і швидше за окремі
-- INSERT, і саме так завантажують дані в реальних ETL-пайплайнах.
-- Нижче, ПІСЛЯ кожної таблиці, залишено як коментар еквівалентну команду
-- COPY — щоб було видно, що саме виконує Python, і щоб той самий SQL-файл
-- можна було прочитати як документацію завантаження.
--
-- Запуск (лише DDL-частину виконує psql; COPY робить Python):
--   psql -d home4331 -f sql/01_create_load.sql
-- =============================================================================

-- Дозволяє повторний запуск файлу без помилок (розробка/демо-стенд).
DROP TABLE IF EXISTS outages CASCADE;
DROP TABLE IF EXISTS readings CASCADE;
DROP TABLE IF EXISTS weather CASCADE;
DROP TABLE IF EXISTS sensors CASCADE;
DROP TABLE IF EXISTS rooms CASCADE;

-- ── rooms: довідник кімнат ────────────────────────────────────────────────
-- room_id — сурогатний ключ; ha_name — англійська назва зони в Home Assistant
-- (як у src/ha_client.py ROOMS_UA); name_ua — назва українською;
-- has_window — чи є вікно (Printernya без вікна; для Tech/Soil вікно
-- не застосовується (NULL) — це не житлові кімнати, а техніка/контроль).
CREATE TABLE rooms (
    room_id     SMALLSERIAL PRIMARY KEY,
    ha_name     TEXT UNIQUE NOT NULL,
    name_ua     TEXT NOT NULL,
    has_window  BOOLEAN
);

-- Python завантажує рядки командою (5 фіксованих рядків, id 1..5):
--   COPY rooms (room_id, ha_name, name_ua, has_window) FROM STDIN WITH (FORMAT csv);

-- ── sensors: довідник датчиків ────────────────────────────────────────────
-- entity_id — ідентифікатор з Home Assistant (первинний ключ);
-- room_id — FK на rooms; metric — вид виміру (temperature/humidity/...);
-- power — тип живлення (mains/battery), з SENSORS у src/ha_client.py.
CREATE TABLE sensors (
    entity_id   TEXT PRIMARY KEY,
    room_id     SMALLINT REFERENCES rooms(room_id),
    metric      TEXT NOT NULL,
    power       TEXT NOT NULL CHECK (power IN ('mains', 'battery'))
);

-- COPY sensors (entity_id, room_id, metric, power) FROM STDIN WITH (FORMAT csv);

-- ── readings: погодинні виміри (M1, data/clean/readings_hourly.csv) ──────
-- УВАГА: завантажуються ВСІ години повного календаря, включно з NULL mean —
-- саме ці "порожні" години і шукають запити 02/03/04 (пропуски, відключення).
-- Якби вантажили лише непорожні рядки, "дірок" у даних просто не було б видно.
CREATE TABLE readings (
    ts          TIMESTAMPTZ NOT NULL,
    entity_id   TEXT NOT NULL REFERENCES sensors(entity_id),
    mean        DOUBLE PRECISION,
    min         DOUBLE PRECISION,
    max         DOUBLE PRECISION,
    stuck       BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (entity_id, ts)
);

-- Індекс по ts окремо (PK вже покриває entity_id+ts, але запити M8
-- часто фільтрують/сортують по ts для одного сенсора або по всій таблиці —
-- окремий індекс по ts прискорює generate_series-джойни в 03/04).
CREATE INDEX ix_readings_ts ON readings (ts);

-- COPY readings (ts, entity_id, mean, min, max, stuck) FROM STDIN WITH (FORMAT csv, NULL '');

-- ── weather: погодинна погода Харкова (Open-Meteo) ───────────────────────
CREATE TABLE weather (
    ts        TIMESTAMPTZ PRIMARY KEY,
    out_temp  DOUBLE PRECISION,
    out_hum   DOUBLE PRECISION,
    out_wind  DOUBLE PRECISION,
    out_cloud DOUBLE PRECISION
);

-- COPY weather (ts, out_temp, out_hum, out_wind, out_cloud) FROM STDIN WITH (FORMAT csv, NULL '');

-- ── outages: відключення, знайдені в Python (M2, results/m2_outages.csv) ─
-- Зберігаємо результат M2 як "еталон", з яким звірятимуться SQL-запити
-- 03_outages.sql (той самий gaps-and-islands, але пораховані в SQL).
CREATE TABLE outages (
    event_id        TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    start_utc       TIMESTAMPTZ NOT NULL,
    end_utc         TIMESTAMPTZ NOT NULL,
    hours           DOUBLE PRECISION NOT NULL,
    weekday_kyiv    TEXT NOT NULL,
    start_hour_kyiv SMALLINT NOT NULL,
    notes           TEXT
);

-- COPY outages (event_id, source, start_utc, end_utc, hours, weekday_kyiv,
--               start_hour_kyiv, notes) FROM STDIN WITH (FORMAT csv);
