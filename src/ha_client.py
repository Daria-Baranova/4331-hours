"""Клієнт Home Assistant для проєкту «4331 година».

Два джерела даних:
  * get_statistics() — погодинна статистика (mean/min/max) через WebSocket,
                       глибина ~6 місяців (з 16.03.2026);
  * get_history()    — детальна історія (кожні ~14 с) через REST, глибина 10 днів.

Токен НІКОЛИ не пишеться в коді: читаємо з файлу (HA_TOKEN_FILE) або зі змінної оточення HA_TOKEN.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
from pathlib import Path

import pandas as pd
import requests
import websockets

HA_URL = os.environ.get("HA_URL", "http://192.168.0.69:8123")
TOKEN_FILE = Path(os.environ.get("HA_TOKEN_FILE", r"N:/documentation/ha-analyics.txt"))
KYIV = "Europe/Kyiv"

# ── Реєстр датчиків проєкту ────────────────────────────────────────────────────
# entity_id -> (зона в HA, метрика, живлення)
SENSORS: dict[str, tuple[str, str, str]] = {
    "sensor.test_product_temperature":     ("Bedroom",    "temperature", "mains"),
    "sensor.test_product_humidity":        ("Bedroom",    "humidity",    "mains"),
    "sensor.test_product_illuminance":     ("Bedroom",    "illuminance", "mains"),
    "sensor.test_product_temperature_2":   ("Kitchen",    "temperature", "mains"),
    "sensor.test_product_humidity_2":      ("Kitchen",    "humidity",    "mains"),
    "sensor.test_product_temperature_3":   ("Printernya", "temperature", "mains"),
    "sensor.test_product_humidity_3":      ("Printernya", "humidity",    "mains"),
    "sensor.tiny_soil_sensor_temperature": ("",           "temperature", "battery"),  # контрольний
}

# Техніка (модуль M5)
TECH: dict[str, str] = {
    "sensor.nas_disk_sda_temperature":   "NAS disk sda, °C",
    "sensor.nas_disk_sdb_temperature":   "NAS disk sdb, °C",
    "sensor.nas_nvme_cache_temperature": "NAS NVMe, °C",
    "sensor.192_168_0_153_cpu_load":     "server CPU load",
    "sensor.pc_cpu_power":               "PC CPU power, W",
    "sensor.pc_cpu_temperature":         "PC CPU temp, °C",
}

ROOMS_UA = {"Bedroom": "житлова кімната", "Kitchen": "кухня", "Printernya": "кладова"}
ROOM_HAS_WINDOW = {"Bedroom": True, "Kitchen": True, "Printernya": False}


# ── Токен ──────────────────────────────────────────────────────────────────────
def ha_token() -> str:
    env = os.environ.get("HA_TOKEN")
    if env:
        return env.strip()
    txt = TOKEN_FILE.read_text(encoding="utf-8")
    cands = [l.strip() for l in txt.splitlines() if re.fullmatch(r"[A-Za-z0-9_.\-]{40,}", l.strip())]
    if not cands:
        raise RuntimeError(f"Токен не знайдено у {TOKEN_FILE}")
    return cands[-1]


def _headers() -> dict:
    return {"Authorization": f"Bearer {ha_token()}", "Content-Type": "application/json"}


def ping() -> bool:
    r = requests.get(f"{HA_URL}/api/", headers=_headers(), timeout=10)
    return r.status_code == 200


# ── Погодинна статистика (WebSocket) ───────────────────────────────────────────
async def _fetch_stats(entity_ids, days: int, period: str = "hour") -> dict:
    start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    ws_url = HA_URL.replace("http", "ws", 1) + "/api/websocket"
    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
        await ws.recv()                                             # auth_required
        await ws.send(json.dumps({"type": "auth", "access_token": ha_token()}))
        if json.loads(await ws.recv())["type"] != "auth_ok":
            raise RuntimeError("HA: авторизація не пройшла")
        await ws.send(json.dumps({
            "id": 1, "type": "recorder/statistics_during_period",
            "start_time": start, "statistic_ids": list(entity_ids),
            "period": period, "types": ["mean", "min", "max"]}))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("id") == 1:
                break
    if not msg.get("success"):
        raise RuntimeError(msg.get("error"))
    return msg["result"]


def get_statistics(entity_ids, days: int = 400, period: str = "hour") -> pd.DataFrame:
    """DataFrame: ts (UTC), entity_id, mean, min, max — один рядок на годину."""
    res = asyncio.run(_fetch_stats(entity_ids, days, period))
    rows = [{"ts": pd.Timestamp(it["start"], unit="ms", tz="UTC"),
             "entity_id": eid, "mean": it.get("mean"), "min": it.get("min"), "max": it.get("max")}
            for eid, items in res.items() for it in items]
    return pd.DataFrame(rows).sort_values(["entity_id", "ts"]).reset_index(drop=True)


# ── Детальна історія (REST, 10 днів) ───────────────────────────────────────────
def get_history(entity_ids, days: int = 10) -> pd.DataFrame:
    """DataFrame: ts (UTC), entity_id, state (текст!) — кожна зміна стану."""
    now = dt.datetime.now(dt.timezone.utc)
    start = (now - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S%z")
    frames = []
    for eid in entity_ids:                       # по одному датчику — надійніше для великих відповідей
        r = requests.get(
            f"{HA_URL}/api/history/period/{start}", headers=_headers(),
            params={"filter_entity_id": eid,
                    "end_time": now.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "minimal_response": ""},
            timeout=300)
        r.raise_for_status()
        rows = [{"ts": pd.Timestamp(p.get("last_updated") or p.get("last_changed")),
                 "entity_id": eid, "state": p["state"]}
                for s in r.json() if s for p in s]
        frames.append(pd.DataFrame(rows))
        print(f"  history {eid}: {len(rows):,} рядків")
    df = pd.concat(frames, ignore_index=True)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.sort_values(["entity_id", "ts"]).reset_index(drop=True)


if __name__ == "__main__":
    print("HA API:", "OK" if ping() else "FAIL")
