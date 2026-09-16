-- =============================================================================
-- 05_cooking.sql — модуль M8, проєкт «4331 година»
-- =============================================================================
-- Що відповідає: скільки разів на кухні готували (за стрибком вологості) —
-- та сама задача, що й M6 (results/m6_cooking.json, погодинне правило).
-- Нова техніка: віконна рамка AVG(...) OVER (ORDER BY ts
-- ROWS BETWEEN 3 PRECEDING AND 1 PRECEDING) + gaps-and-islands для склеювання
-- послідовних "гарячих" годин в одну подію.
--
-- ПРИМІТКА про рамку: у специфікації M8 (розділ "Приклад") наведено ілюстрацію
-- з ROWS BETWEEN 3 PRECEDING AND CURRENT ROW — це загальний приклад техніки.
-- Тут навмисно взято означення з фактичного модуля M6 (src/cooking.py,
-- detect_events_hourly): "приріст = кухонна вологість МІНУС середнє за
-- 3 ГОДИНИ, що передують поточній" (тобто поточна година в середнє НЕ входить,
-- інакше "приріст" рахувався б відносно самого стрибка) — саме
-- ROWS BETWEEN 3 PRECEDING AND 1 PRECEDING. Це зроблено свідомо, щоб число
-- подій (26) збігалося і в Python, і в SQL.
--
-- Запуск: psql -d home4331 -f sql/05_cooking.sql
-- =============================================================================

-- 1) Погодинна вологість кухні на повному календарі (ті самі години, що і
--    в data/clean/hours_wide.csv), з рамкою AVG за 3 попередні години.
WITH calendar AS (
    SELECT generate_series(MIN(ts), MAX(ts), INTERVAL '1 hour') AS ts FROM readings
),
kitchen AS (
    SELECT c.ts, r.mean AS kitchen_hum
    FROM calendar c
    LEFT JOIN readings r
           ON r.ts = c.ts AND r.entity_id = 'sensor.test_product_humidity_2'
),
windowed AS (
    SELECT ts, kitchen_hum,
           AVG(kitchen_hum) OVER (ORDER BY ts
               ROWS BETWEEN 3 PRECEDING AND 1 PRECEDING)      AS baseline_hum,
           kitchen_hum - AVG(kitchen_hum) OVER (ORDER BY ts
               ROWS BETWEEN 3 PRECEDING AND 1 PRECEDING)      AS rise_pp
    FROM kitchen
),
flagged AS (
    SELECT ts, kitchen_hum, baseline_hum, rise_pp,
           (rise_pp >= 6 AND kitchen_hum IS NOT NULL)         AS is_cooking
    FROM windowed
),
-- 2) gaps-and-islands лише для "гарячих" годин: сусідні позначені години
--    склеюються в одну подію.
islands AS (
    SELECT ts, kitchen_hum, baseline_hum, rise_pp,
           ts - (ROW_NUMBER() OVER (ORDER BY ts) * INTERVAL '1 hour') AS grp
    FROM flagged
    WHERE is_cooking
)
SELECT MIN(ts)                          AS start_utc,
       MAX(ts) + INTERVAL '1 hour'      AS end_utc,
       COUNT(*)                         AS n_hours,
       ROUND(MIN(baseline_hum)::numeric, 2) AS baseline_hum,
       ROUND(MAX(kitchen_hum)::numeric, 2)  AS peak_hum,
       ROUND(MAX(rise_pp)::numeric, 2)      AS rise_pp
FROM islands
GROUP BY grp
ORDER BY start_utc;

-- 3) Скільки всього подій — порівняти з n_events_hourly=26 у m6_cooking.json.
WITH calendar AS (
    SELECT generate_series(MIN(ts), MAX(ts), INTERVAL '1 hour') AS ts FROM readings
),
kitchen AS (
    SELECT c.ts, r.mean AS kitchen_hum
    FROM calendar c
    LEFT JOIN readings r
           ON r.ts = c.ts AND r.entity_id = 'sensor.test_product_humidity_2'
),
windowed AS (
    SELECT ts, kitchen_hum,
           kitchen_hum - AVG(kitchen_hum) OVER (ORDER BY ts
               ROWS BETWEEN 3 PRECEDING AND 1 PRECEDING) AS rise_pp
    FROM kitchen
),
flagged AS (
    SELECT ts FROM windowed WHERE rise_pp >= 6 AND kitchen_hum IS NOT NULL
),
islands AS (
    SELECT ts - (ROW_NUMBER() OVER (ORDER BY ts) * INTERVAL '1 hour') AS grp
    FROM flagged
)
SELECT COUNT(*) AS n_events_hourly FROM (SELECT grp FROM islands GROUP BY grp) g;
