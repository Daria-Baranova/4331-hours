-- =============================================================================
-- 04_climate.sql — модуль M8, проєкт «4331 година»
-- =============================================================================
-- Що відповідає: клімат по кімнатах у розрізі місяців і годин доби, порівняння
-- з вуличною погодою, комфортні % — та сама задача, що й M3 (results/m3_*).
-- Нові техніки: DATE_TRUNC, generate_series + FULL OUTER JOIN для пропусків,
-- JOIN з погодою, EXTRACT(HOUR ...) для циклу доби, COUNT(*) FILTER (WHERE ...).
--
-- Застряглі години (stuck) виключаємо скрізь так само, як у Python (M3):
-- вони псують середнє, хоч технічно й не NULL.
-- Місяць/година рахуються за КИЇВСЬКИМ часом (ts AT TIME ZONE 'Europe/Kyiv'),
-- бо саме так рахувала Python-версія (ts_kyiv.strftime("%Y-%m") / .hour).
--
-- Запуск: psql -d home4331 -f sql/04_climate.sql
-- =============================================================================

-- Спільна "довга" таблиця: (ts, кімната, метрика, значення) лише для 3 житлових
-- кімнат, лише temperature/humidity, застряглі години -> NULL (і одразу відкинуті).
-- WITH-запит нижче повторюється в кожному пункті — це навмисно: кожен пункт
-- має лишатись самостійним запитом, який можна скопіювати й запустити окремо.

-- 1) Місяць × кімната: середня/мін/макс температура, середня вологість.
--    Порівняти з results/m3_monthly.csv (напр. 2026-03, Bedroom: n=342, mean=22.67).
WITH climate AS (
    SELECT r.ts, ro.ha_name AS room, s.metric,
           CASE WHEN r.stuck THEN NULL ELSE r.mean END AS value
    FROM readings r
    JOIN sensors s ON s.entity_id = r.entity_id
    JOIN rooms   ro ON ro.room_id = s.room_id
    WHERE ro.ha_name IN ('Bedroom', 'Kitchen', 'Printernya')
      AND s.metric IN ('temperature', 'humidity')
)
SELECT DATE_TRUNC('month', ts AT TIME ZONE 'Europe/Kyiv')::date AS month,
       room,
       COUNT(*)   FILTER (WHERE metric = 'temperature')                    AS n_hours_temp,
       ROUND(AVG(value) FILTER (WHERE metric = 'temperature')::numeric, 2) AS temp_mean,
       MIN(value) FILTER (WHERE metric = 'temperature')                    AS temp_min,
       MAX(value) FILTER (WHERE metric = 'temperature')                    AS temp_max,
       ROUND(AVG(value) FILTER (WHERE metric = 'humidity')::numeric, 2)    AS hum_mean
FROM climate
WHERE value IS NOT NULL
GROUP BY month, room
ORDER BY month, room;

-- 2) Пропущені години для одного сенсора (Kitchen, температура) відносно
--    повного спільного календаря — приклад із розділу M8 специфікації,
--    FULL OUTER JOIN замість LEFT JOIN, щоб побачити обидва боки одразу.
WITH calendar AS (
    SELECT generate_series(MIN(ts), MAX(ts), INTERVAL '1 hour') AS ts FROM readings
),
kitchen_temp AS (
    SELECT ts, mean FROM readings WHERE entity_id = 'sensor.test_product_temperature_2'
)
SELECT c.ts AS calendar_ts, k.ts AS reading_ts, k.mean
FROM calendar c
FULL OUTER JOIN kitchen_temp k ON k.ts = c.ts
WHERE k.ts IS NULL OR k.mean IS NULL
ORDER BY c.ts
LIMIT 20;

-- Скільки таких годин загалом (для звірки: 4402 - 3942 = 460 годин Kitchen
-- ще не існувало влітку і 3942-2679=1263 год без виміру всередині періоду).
WITH calendar AS (
    SELECT generate_series(MIN(ts), MAX(ts), INTERVAL '1 hour') AS ts FROM readings
),
kitchen_temp AS (
    SELECT ts, mean FROM readings WHERE entity_id = 'sensor.test_product_temperature_2'
)
SELECT
    COUNT(*) FILTER (WHERE k.ts IS NULL)                 AS hours_sensor_not_installed,
    COUNT(*) FILTER (WHERE k.ts IS NOT NULL AND k.mean IS NULL) AS hours_present_but_null
FROM calendar c
FULL OUTER JOIN kitchen_temp k ON k.ts = c.ts;

-- 3) JOIN з погодою: погодинна температура в кімнаті поруч із вуличною +
--    кореляція (Bedroom, найповніший ряд). Порівняти напрямок з bedroom_lag_corr.
SELECT ROUND(CORR(r.mean, w.out_temp)::numeric, 3) AS corr_bedroom_outdoor
FROM readings r
JOIN weather w ON w.ts = r.ts
WHERE r.entity_id = 'sensor.test_product_temperature' AND NOT r.stuck;

-- 4) Добовий цикл: середня температура по годині доби (Київ) для кожної кімнати.
--    Порівняти з results/m3_daily_cycle.csv (hour=0: Bedroom≈23.973).
WITH climate AS (
    SELECT r.ts, ro.ha_name AS room,
           CASE WHEN r.stuck THEN NULL ELSE r.mean END AS temp
    FROM readings r
    JOIN sensors s ON s.entity_id = r.entity_id
    JOIN rooms   ro ON ro.room_id = s.room_id
    WHERE ro.ha_name IN ('Bedroom', 'Kitchen', 'Printernya') AND s.metric = 'temperature'
)
SELECT EXTRACT(HOUR FROM ts AT TIME ZONE 'Europe/Kyiv')::int AS hour_kyiv,
       room,
       ROUND(AVG(temp)::numeric, 3) AS avg_temp
FROM climate
WHERE temp IS NOT NULL
GROUP BY hour_kyiv, room
ORDER BY hour_kyiv, room;

-- 5) Комфортні %: 20–24 °C і 30–60 % вологості (ті самі межі, що в M3).
--    Порівняти bedroom_comfort_temp_ok_pct=42.3 / bedroom_comfort_hum_ok_pct=95.8.
WITH climate AS (
    SELECT r.ts, ro.ha_name AS room, s.metric,
           CASE WHEN r.stuck THEN NULL ELSE r.mean END AS value
    FROM readings r
    JOIN sensors s ON s.entity_id = r.entity_id
    JOIN rooms   ro ON ro.room_id = s.room_id
    WHERE ro.ha_name IN ('Bedroom', 'Kitchen', 'Printernya')
      AND s.metric IN ('temperature', 'humidity')
      AND (CASE WHEN r.stuck THEN NULL ELSE r.mean END) IS NOT NULL
)
SELECT room,
       COUNT(*) FILTER (WHERE metric = 'temperature')                                        AS n_hours_temp,
       ROUND(100.0 * COUNT(*) FILTER (WHERE metric = 'temperature' AND value < 20)
             / NULLIF(COUNT(*) FILTER (WHERE metric = 'temperature'), 0), 1)                  AS too_cold_pct,
       ROUND(100.0 * COUNT(*) FILTER (WHERE metric = 'temperature' AND value BETWEEN 20 AND 24)
             / NULLIF(COUNT(*) FILTER (WHERE metric = 'temperature'), 0), 1)                  AS temp_ok_pct,
       ROUND(100.0 * COUNT(*) FILTER (WHERE metric = 'temperature' AND value > 24)
             / NULLIF(COUNT(*) FILTER (WHERE metric = 'temperature'), 0), 1)                  AS too_hot_pct,
       COUNT(*) FILTER (WHERE metric = 'humidity')                                            AS n_hours_hum,
       ROUND(100.0 * COUNT(*) FILTER (WHERE metric = 'humidity' AND value BETWEEN 30 AND 60)
             / NULLIF(COUNT(*) FILTER (WHERE metric = 'humidity'), 0), 1)                      AS hum_ok_pct
FROM climate
GROUP BY room
ORDER BY room;
