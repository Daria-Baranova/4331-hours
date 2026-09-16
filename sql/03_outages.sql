-- =============================================================================
-- 03_outages.sql — модуль M8, проєкт «4331 година»
-- =============================================================================
-- Що відповідає: скільки було відключень світла і на скільки годин — та сама
-- задача, що й у Python (M2, results/m2_outages.csv), тепер порахована в SQL.
-- Нові техніки: LAG, класичний gaps-and-islands (групування послідовних
-- рядків трюком "номер рядка мінус номер у групі"), INTERVAL, FULL OUTER JOIN.
--
-- Логіка (спрощена, але чесна відносно M2 — src/outages.py):
-- "мовчать усі доступні розеткові датчики" = 4 сигнали живлення:
--   bedroom_temp (sensor.test_product_temperature),
--   printernya_temp (sensor.test_product_temperature_3),
--   server_cpu_load (sensor.192_168_0_153_cpu_load),
--   nas_sda_temp (sensor.nas_disk_sda_temperature).
-- Година вважається "світла нема", якщо mean IS NULL і сенсор не "застряг"
-- (stuck) у КОЖНОГО з чотирьох. Для сенсора, який ще не встановлено на цю
-- годину, у readings просто немає рядка — LEFT JOIN дає той самий NULL,
-- тож формула працює однаково і для "ще не стоїть", і для "замовк":
-- ранній OUT-001 (тільки bedroom_temp був живий свідком у березні) знайдеться
-- так само, бо для трьох ще не встановлених сенсорів LEFT JOIN теж дає NULL.
-- Це і є "найпростіша чесна версія" — без окремого відстеження дати монтажу.
--
-- Запуск: psql -d home4331 -f sql/03_outages.sql
-- =============================================================================

-- 1) Повний календар годин (той самий діапазон, що і в readings) +
--    по кожній із 4 "розеткових" точок: чи мовчить вона цю годину.
WITH calendar AS (
    SELECT generate_series(MIN(ts), MAX(ts), INTERVAL '1 hour') AS ts FROM readings
),
witness AS (
    SELECT c.ts,
           (r1.mean IS NULL AND NOT COALESCE(r1.stuck, FALSE)) AS silent_bedroom,
           (r2.mean IS NULL AND NOT COALESCE(r2.stuck, FALSE)) AS silent_printernya,
           (r3.mean IS NULL AND NOT COALESCE(r3.stuck, FALSE)) AS silent_server,
           (r4.mean IS NULL AND NOT COALESCE(r4.stuck, FALSE)) AS silent_nas
    FROM calendar c
    LEFT JOIN readings r1 ON r1.ts = c.ts AND r1.entity_id = 'sensor.test_product_temperature'
    LEFT JOIN readings r2 ON r2.ts = c.ts AND r2.entity_id = 'sensor.test_product_temperature_3'
    LEFT JOIN readings r3 ON r3.ts = c.ts AND r3.entity_id = 'sensor.192_168_0_153_cpu_load'
    LEFT JOIN readings r4 ON r4.ts = c.ts AND r4.entity_id = 'sensor.nas_disk_sda_temperature'
),
-- 2) Години-кандидати: усі чотири мовчать одночасно.
off_hours AS (
    SELECT ts
    FROM witness
    WHERE silent_bedroom AND silent_printernya AND silent_server AND silent_nas
),
-- 3) gaps-and-islands: у послідовних годинах різниця
--    (номер_рядка_за_часом − номер_рядка_у_вибірці) стала — це і є "острів".
--    (Класичний альтернативний варіант — LAG(ts) і розрив, коли
--    ts - LAG(ts) > INTERVAL '1 hour'; тут узято варіант із ROW_NUMBER,
--    бо він же знадобиться і для рамки в 05_cooking.sql.)
islands AS (
    SELECT ts,
           ts - (ROW_NUMBER() OVER (ORDER BY ts) * INTERVAL '1 hour') AS grp
    FROM off_hours
)
SELECT MIN(ts)                                   AS start_utc,
       MAX(ts) + INTERVAL '1 hour'               AS end_utc,
       COUNT(*)                                  AS hours
FROM islands
GROUP BY grp
ORDER BY start_utc;

-- 4) Звірка з таблицею outages (заповненою з Python, M2): FULL OUTER JOIN
--    по перетину інтервалів — рядок без пари з обох боків = розбіжність.
WITH calendar AS (
    SELECT generate_series(MIN(ts), MAX(ts), INTERVAL '1 hour') AS ts FROM readings
),
witness AS (
    SELECT c.ts,
           (r1.mean IS NULL AND NOT COALESCE(r1.stuck, FALSE)) AS silent_bedroom,
           (r2.mean IS NULL AND NOT COALESCE(r2.stuck, FALSE)) AS silent_printernya,
           (r3.mean IS NULL AND NOT COALESCE(r3.stuck, FALSE)) AS silent_server,
           (r4.mean IS NULL AND NOT COALESCE(r4.stuck, FALSE)) AS silent_nas
    FROM calendar c
    LEFT JOIN readings r1 ON r1.ts = c.ts AND r1.entity_id = 'sensor.test_product_temperature'
    LEFT JOIN readings r2 ON r2.ts = c.ts AND r2.entity_id = 'sensor.test_product_temperature_3'
    LEFT JOIN readings r3 ON r3.ts = c.ts AND r3.entity_id = 'sensor.192_168_0_153_cpu_load'
    LEFT JOIN readings r4 ON r4.ts = c.ts AND r4.entity_id = 'sensor.nas_disk_sda_temperature'
),
off_hours AS (
    SELECT ts FROM witness
    WHERE silent_bedroom AND silent_printernya AND silent_server AND silent_nas
),
islands AS (
    SELECT ts, ts - (ROW_NUMBER() OVER (ORDER BY ts) * INTERVAL '1 hour') AS grp
    FROM off_hours
),
sql_events AS (
    SELECT MIN(ts) AS start_utc, MAX(ts) + INTERVAL '1 hour' AS end_utc, COUNT(*) AS hours
    FROM islands GROUP BY grp
)
SELECT o.event_id, o.start_utc AS py_start, o.hours AS py_hours,
       s.start_utc AS sql_start, s.hours AS sql_hours,
       (o.start_utc = s.start_utc AND o.hours = s.hours) AS match
FROM outages o
FULL OUTER JOIN sql_events s ON o.start_utc = s.start_utc
ORDER BY COALESCE(o.start_utc, s.start_utc);

-- 5) Підсумок: кількість подій, сумарні години, uptime % — звірити з
--    n_events_total=5, total_hours=35.0, uptime_pct=99.205 у m2_outages.json.
WITH calendar AS (
    SELECT generate_series(MIN(ts), MAX(ts), INTERVAL '1 hour') AS ts FROM readings
),
witness AS (
    SELECT c.ts,
           (r1.mean IS NULL AND NOT COALESCE(r1.stuck, FALSE)) AS silent_bedroom,
           (r2.mean IS NULL AND NOT COALESCE(r2.stuck, FALSE)) AS silent_printernya,
           (r3.mean IS NULL AND NOT COALESCE(r3.stuck, FALSE)) AS silent_server,
           (r4.mean IS NULL AND NOT COALESCE(r4.stuck, FALSE)) AS silent_nas
    FROM calendar c
    LEFT JOIN readings r1 ON r1.ts = c.ts AND r1.entity_id = 'sensor.test_product_temperature'
    LEFT JOIN readings r2 ON r2.ts = c.ts AND r2.entity_id = 'sensor.test_product_temperature_3'
    LEFT JOIN readings r3 ON r3.ts = c.ts AND r3.entity_id = 'sensor.192_168_0_153_cpu_load'
    LEFT JOIN readings r4 ON r4.ts = c.ts AND r4.entity_id = 'sensor.nas_disk_sda_temperature'
),
off_hours AS (
    SELECT ts FROM witness
    WHERE silent_bedroom AND silent_printernya AND silent_server AND silent_nas
),
islands AS (
    SELECT ts, ts - (ROW_NUMBER() OVER (ORDER BY ts) * INTERVAL '1 hour') AS grp
    FROM off_hours
),
sql_events AS (
    SELECT COUNT(*) AS hours FROM islands GROUP BY grp
)
SELECT (SELECT COUNT(*) FROM sql_events)                                    AS n_events,
       (SELECT SUM(hours) FROM sql_events)                                  AS total_off_hours,
       (SELECT COUNT(*) FROM calendar)                                      AS total_calendar_hours,
       ROUND(100.0 * (1 - (SELECT SUM(hours) FROM sql_events)::numeric
                          / (SELECT COUNT(*) FROM calendar)), 3)            AS uptime_pct;
