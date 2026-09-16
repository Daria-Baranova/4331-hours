-- =============================================================================
-- 02_validation.sql — модуль M8, проєкт «4331 година»
-- =============================================================================
-- Що відповідає: чи є в завантажених даних NULL/дублікати/викиди, скільки
-- реально даних по кожному сенсору (% покриття) і скільки годин "застряглі".
-- Нові техніки: COUNT(*) FILTER (WHERE ...), GROUP BY ... HAVING,
-- generate_series() для повного календаря, LEFT JOIN для пошуку пропусків.
--
-- Запуск: psql -d home4331 -f sql/02_validation.sql
-- =============================================================================

-- 1) Скільки всього рядків і скільки з них без виміру (NULL mean) —
--    порівняти з results/m1_quality.json (hourly_rows=53946).
SELECT
    COUNT(*)                                   AS total_rows,
    COUNT(*) FILTER (WHERE mean IS NULL)       AS null_mean_rows,
    ROUND(100.0 * COUNT(*) FILTER (WHERE mean IS NULL) / COUNT(*), 1) AS null_mean_pct
FROM readings;

-- 2) Дублікати первинного ключа (entity_id, ts) — має бути 0, бо PK це
--    вже і так гарантує; запит лишається як самостійна перевірка "з нуля"
--    (так, як шукали б дублікати в сирих даних без PK).
SELECT entity_id, ts, COUNT(*) AS n
FROM readings
GROUP BY entity_id, ts
HAVING COUNT(*) > 1;

-- 3) Значення поза фізичним діапазоном (грубий здоровий глузд): температура
--    поза -30..60 °C, вологість поза 0..100 % — сигнал биту датчика/парсингу.
SELECT r.entity_id, s.metric, r.ts, r.mean
FROM readings r
JOIN sensors s USING (entity_id)
WHERE (s.metric = 'temperature' AND (r.mean < -30 OR r.mean > 60))
   OR (s.metric = 'humidity'    AND (r.mean < 0   OR r.mean > 100));

-- 4) Покриття % по кожному сенсору. ВАЖЛИВО: календар у Python (M1) для
--    кожного датчика рахується від ЙОГО ВЛАСНОГО першого запису до
--    спільного максимуму (пізніше встановлений датчик не штрафується за
--    місяці, коли його фізично ще не було) — і саме такий календар уже
--    "запечений" у readings: у кожного entity_id рівно один рядок на
--    годину від його першого до останнього моменту в таблиці. Тому
--    calendar_hours = просто COUNT(*) по цьому entity_id, без generate_series.
--    (generate_series для повного спільного календаря використовується
--    нижче, у 03/04, де потрібен саме єдиний календар для всіх сенсорів.)
--    Порівняти з coverage_bedroom_temp / coverage_kitchen_temp у m1_quality.json.
SELECT entity_id,
       COUNT(*)        AS calendar_hours,
       COUNT(mean)     AS filled_hours,
       ROUND(100.0 * COUNT(mean) / COUNT(*), 1) AS coverage_pct
FROM readings
GROUP BY entity_id
ORDER BY coverage_pct;

-- 5) Застряглі години (stuck = TRUE) — скільки і по яких сенсорах;
--    порівняти stuck_hours=384 у m1_quality.json (сума по всіх сенсорах,
--    tiny_soil включно — там і "живе" застрягання батарейного датчика).
SELECT entity_id, COUNT(*) FILTER (WHERE stuck) AS stuck_hours
FROM readings
GROUP BY entity_id
HAVING COUNT(*) FILTER (WHERE stuck) > 0
ORDER BY stuck_hours DESC;

SELECT COUNT(*) FILTER (WHERE stuck) AS stuck_hours_total FROM readings;
