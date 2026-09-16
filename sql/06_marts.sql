-- =============================================================================
-- 06_marts.sql — модуль M8, проєкт «4331 година»
-- =============================================================================
-- Що робить: підсумкова вітрина даних для дашборда — 4 представлення (VIEW),
-- які ховають усю складність попередніх файлів за простими SELECT * FROM.
-- Нові техніки: CREATE OR REPLACE VIEW, percentile_cont (медіана),
-- умовна агрегація (FILTER / CASE у GROUP BY), ROUND, CASE-мітки.
--
-- Запуск: psql -d home4331 -f sql/06_marts.sql
-- Перегляд: SELECT * FROM v_dashboard_kpis;
-- =============================================================================

-- 1) v_room_hourly: погодинні температура/вологість трьох житлових кімнат,
--    одним рядком на (година, кімната) — застряглі показники вже прибрані.
--    Це "довга, але вже прибрана" таблиця, на якій будуються наступні вітрини.
CREATE OR REPLACE VIEW v_room_hourly AS
SELECT r.ts,
       ro.ha_name AS room,
       ro.name_ua AS room_ua,
       MAX(CASE WHEN s.metric = 'temperature' AND NOT r.stuck THEN r.mean END) AS temp,
       MAX(CASE WHEN s.metric = 'humidity'    AND NOT r.stuck THEN r.mean END) AS hum
FROM readings r
JOIN sensors s ON s.entity_id = r.entity_id
JOIN rooms   ro ON ro.room_id = s.room_id
WHERE ro.ha_name IN ('Bedroom', 'Kitchen', 'Printernya')
GROUP BY r.ts, ro.ha_name, ro.name_ua;

-- 2) v_monthly_climate: місяць × кімната (див. 04_climate.sql, п.1), але як
--    вітрина — дашборд просто робить SELECT * FROM v_monthly_climate.
CREATE OR REPLACE VIEW v_monthly_climate AS
SELECT DATE_TRUNC('month', ts AT TIME ZONE 'Europe/Kyiv')::date AS month,
       room, room_ua,
       COUNT(temp)                          AS n_hours_temp,
       ROUND(AVG(temp)::numeric, 2)         AS temp_mean,
       MIN(temp)                            AS temp_min,
       MAX(temp)                            AS temp_max,
       ROUND(AVG(hum)::numeric, 2)          AS hum_mean
FROM v_room_hourly
GROUP BY month, room, room_ua
ORDER BY month, room;

-- 3) v_outage_summary: підсумок по відключеннях, з медіаною (percentile_cont) —
--    медіана стійкіша за середнє, коли подій мало і є один довгий викид.
CREATE OR REPLACE VIEW v_outage_summary AS
SELECT COUNT(*)                                                     AS n_events,
       SUM(hours)                                                   AS total_hours,
       ROUND(AVG(hours)::numeric, 2)                                AS mean_hours,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY hours)            AS median_hours,
       MAX(hours)                                                   AS max_hours,
       MIN(start_utc)                                                AS period_start_utc,
       MAX(end_utc)                                                  AS period_end_utc
FROM outages;

-- 4) v_dashboard_kpis: один рядок головних показників + текстові мітки
--    (CASE) — саме ці колонки читає HTML-дашборд, без жодної логіки в JS.
CREATE OR REPLACE VIEW v_dashboard_kpis AS
WITH calendar_hours AS (
    SELECT COUNT(*) AS n FROM (
        SELECT generate_series(MIN(ts), MAX(ts), INTERVAL '1 hour') FROM readings
    ) AS t
),
outage AS (SELECT * FROM v_outage_summary),
bedroom_comfort AS (
    SELECT ROUND(100.0 * COUNT(*) FILTER (WHERE mean BETWEEN 20 AND 24)
                 / NULLIF(COUNT(*), 0), 1) AS comfort_temp_ok_pct
    FROM readings
    WHERE entity_id = 'sensor.test_product_temperature' AND NOT stuck AND mean IS NOT NULL
),
kitchen_cov AS (
    SELECT ROUND(100.0 * COUNT(mean) / COUNT(*), 1) AS coverage_pct
    FROM readings WHERE entity_id = 'sensor.test_product_humidity_2'
)
SELECT
    outage.n_events                                             AS outage_events,
    outage.total_hours                                          AS outage_hours,
    outage.median_hours                                         AS outage_median_hours,
    ROUND((100.0 * (1 - outage.total_hours / calendar_hours.n))::numeric, 3) AS uptime_pct,
    CASE WHEN ROUND((100.0 * (1 - outage.total_hours / calendar_hours.n))::numeric, 3) >= 99
              THEN 'стабільно' ELSE 'нестабільно' END            AS uptime_label,
    bedroom_comfort.comfort_temp_ok_pct                          AS bedroom_comfort_pct,
    CASE WHEN bedroom_comfort.comfort_temp_ok_pct >= 50 THEN 'комфортно'
         ELSE 'частіше спекотно, ніж комфортно' END              AS bedroom_comfort_label,
    kitchen_cov.coverage_pct                                     AS kitchen_sensor_coverage_pct
FROM outage, calendar_hours, bedroom_comfort, kitchen_cov;

-- Перевірка, що всі 4 вітрини читаються без помилок:
SELECT * FROM v_room_hourly LIMIT 5;
SELECT * FROM v_monthly_climate LIMIT 5;
SELECT * FROM v_outage_summary;
SELECT * FROM v_dashboard_kpis;
