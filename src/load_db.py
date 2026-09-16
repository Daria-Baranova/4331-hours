"""Завантажувач чистих даних у PostgreSQL (модуль M8, проєкт «4331 година»).

Створює базу `home4331` (якщо її ще немає), виконує DDL з
`sql/01_create_load.sql` і завантажує 5 таблиць командою
`COPY ... FROM STDIN` (psycopg2 `copy_expert`) із `data/clean/*.csv`
та `results/m2_outages.csv`.

З'єднання береться ЛИШЕ зі змінних оточення — PGHOST/PGPORT/PGUSER/
/PGPASSWORD/PGDATABASE (їх нативно читає і psycopg2, і psql). Пароль
у коді ніде не зберігається: на реальному сервері власник просто
виставляє PGPASSWORD і запускає той самий скрипт.

Запуск:
    PYTHONUTF8=1 python -m src.load_db
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pandas as pd
import psycopg2

from src.ha_client import SENSORS, ROOMS_UA, ROOM_HAS_WINDOW

ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = ROOT / "sql"
CLEAN = ROOT / "data" / "clean"
RESULTS = ROOT / "results"

DBNAME = os.environ.get("PGDATABASE", "home4331")

# ── довідник кімнат: 3 житлові (з вікном/без) + Tech/Soil — техніка й ─────
# контрольний датчик не належать до жодної житлової кімнати, але щоб не
# тримати room_id NULL-able у practice-запитах, для них теж є рядки.
ROOMS = [
    # (room_id, ha_name,     name_ua,                        has_window)
    (1,        "Bedroom",    ROOMS_UA["Bedroom"],             ROOM_HAS_WINDOW["Bedroom"]),
    (2,        "Kitchen",    ROOMS_UA["Kitchen"],             ROOM_HAS_WINDOW["Kitchen"]),
    (3,        "Printernya", ROOMS_UA["Printernya"],          ROOM_HAS_WINDOW["Printernya"]),
    (4,        "Tech",       "техніка (сервер/NAS/принтер)",  None),
    (5,        "Soil",       "контроль (батарея квітки)",     None),
]
ROOM_ID_BY_NAME = {r[1]: r[0] for r in ROOMS}

# entity_id -> power ("mains"/"battery"), з SENSORS у ha_client.py;
# усе, чого там немає (TECH: NAS/сервер/ПК), живиться від мережі.
POWER_BY_ENTITY = {eid: power for eid, (_room, _metric, power) in SENSORS.items()}


def _conn(dbname: str, autocommit: bool = False):
    conn = psycopg2.connect(dbname=dbname)  # host/port/user/password — з PG*-змінних оточення
    conn.autocommit = autocommit
    return conn


def ensure_database() -> None:
    """CREATE DATABASE home4331, якщо її ще немає (з'єднуємось до `postgres`)."""
    conn = _conn("postgres", autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DBNAME,))
            if cur.fetchone() is None:
                cur.execute(f'CREATE DATABASE "{DBNAME}"')
                print(f"База {DBNAME} створена.")
            else:
                print(f"База {DBNAME} вже існує.")
    finally:
        conn.close()


def run_ddl(conn) -> None:
    """Виконує DDL-частину sql/01_create_load.sql (COPY там лише в коментарях)."""
    ddl = (SQL_DIR / "01_create_load.sql").read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(ddl)
    conn.commit()
    print("DDL виконано: rooms, sensors, readings, weather, outages.")


def _copy_buffer(cur, table: str, columns: list[str], df: pd.DataFrame) -> None:
    buf = io.StringIO()
    df.to_csv(buf, index=False, header=False, na_rep="")
    buf.seek(0)
    cols = ", ".join(columns)
    cur.copy_expert(f"COPY {table} ({cols}) FROM STDIN WITH (FORMAT csv, NULL '')", buf)


def load_rooms(conn) -> int:
    df = pd.DataFrame(ROOMS, columns=["room_id", "ha_name", "name_ua", "has_window"])
    with conn.cursor() as cur:
        _copy_buffer(cur, "rooms", list(df.columns), df)
        cur.execute("SELECT setval(pg_get_serial_sequence('rooms', 'room_id'), (SELECT MAX(room_id) FROM rooms))")
    conn.commit()
    return len(df)


def load_sensors(conn) -> int:
    # Довідник (entity_id, room, metric) беремо з readings_hourly.csv —
    # це вже очищена, узгоджена мапа M1 (там і TECH-сенсори з їхнім room="Tech").
    reg = (pd.read_csv(CLEAN / "readings_hourly.csv", usecols=["entity_id", "room", "metric"])
             .drop_duplicates()
             .reset_index(drop=True))
    reg["room_id"] = reg["room"].map(ROOM_ID_BY_NAME)
    reg["power"] = reg["entity_id"].map(POWER_BY_ENTITY).fillna("mains")
    df = reg[["entity_id", "room_id", "metric", "power"]]
    with conn.cursor() as cur:
        _copy_buffer(cur, "sensors", list(df.columns), df)
    conn.commit()
    return len(df)


def load_readings(conn) -> int:
    # Усі рядки повного погодинного календаря, включно з NULL mean/min/max —
    # саме "порожні" години шукають запити 02_validation.sql / 03_outages.sql.
    df = pd.read_csv(CLEAN / "readings_hourly.csv",
                      usecols=["ts", "entity_id", "mean", "min", "max", "stuck"])
    with conn.cursor() as cur:
        _copy_buffer(cur, "readings", list(df.columns), df)
    conn.commit()
    return len(df)


def load_weather(conn) -> int:
    path = CLEAN / "weather_hourly.csv"
    df = pd.read_csv(path)  # колонки вже точно збігаються з таблицею weather
    with conn.cursor() as cur:
        with open(path, "r", encoding="utf-8") as f:
            next(f)  # пропустити заголовок — імена колонок передаємо явно
            cur.copy_expert(
                "COPY weather (ts, out_temp, out_hum, out_wind, out_cloud) "
                "FROM STDIN WITH (FORMAT csv, NULL '')", f)
    conn.commit()
    return len(df)


def load_outages(conn) -> int:
    cols = ["event_id", "source", "start_utc", "end_utc", "hours",
            "weekday_kyiv", "start_hour_kyiv", "notes"]
    df = pd.read_csv(RESULTS / "m2_outages.csv", usecols=cols)[cols]
    with conn.cursor() as cur:
        _copy_buffer(cur, "outages", cols, df)
    conn.commit()
    return len(df)


def main() -> None:
    ensure_database()
    conn = _conn(DBNAME)
    try:
        run_ddl(conn)
        counts = {
            "rooms": load_rooms(conn),
            "sensors": load_sensors(conn),
            "readings": load_readings(conn),
            "weather": load_weather(conn),
            "outages": load_outages(conn),
        }
    finally:
        conn.close()

    print("\nЗавантажено рядків:")
    for table, n in counts.items():
        print(f"  {table:10s} {n:>7,}".replace(",", " "))


if __name__ == "__main__":
    sys.exit(main())
