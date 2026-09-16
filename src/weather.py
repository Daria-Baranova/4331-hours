"""Погода Харкова з Open-Meteo (безкоштовно, без ключа).

ВАЖЛИВО: просимо timezone=UTC. З timezone=Europe/Kyiv перехід на літній час
(29.03.2026, година 03:00 не існує) ламає tz_localize у pandas.
У київський час переводимо вже всередині pandas: df.ts.dt.tz_convert("Europe/Kyiv").
"""
from __future__ import annotations

import pandas as pd
import requests

LAT, LON = 49.99, 36.23                     # Харків
HOURLY = "temperature_2m,relative_humidity_2m,wind_speed_10m,cloud_cover"
RENAME = {"temperature_2m": "out_temp", "relative_humidity_2m": "out_hum",
          "wind_speed_10m": "out_wind", "cloud_cover": "out_cloud"}


def _to_df(hourly: dict) -> pd.DataFrame:
    return (pd.DataFrame(hourly)
              .assign(ts=lambda d: pd.to_datetime(d.time).dt.tz_localize("UTC"))
              .drop(columns="time")
              .rename(columns=RENAME))


def get_weather(start: str, end: str) -> pd.DataFrame:
    """Архів: погодинна погода start..end (YYYY-MM-DD). Останні 1-2 дні можуть бути порожні."""
    r = requests.get("https://archive-api.open-meteo.com/v1/archive", timeout=120,
                     params={"latitude": LAT, "longitude": LON,
                             "start_date": start, "end_date": end,
                             "hourly": HOURLY, "timezone": "UTC"})
    r.raise_for_status()
    return _to_df(r.json()["hourly"])


def get_weather_recent(past_days: int = 7) -> pd.DataFrame:
    """Останні дні з forecast-API (архів відстає на ~2 дні) — щоб закрити хвіст."""
    r = requests.get("https://api.open-meteo.com/v1/forecast", timeout=60,
                     params={"latitude": LAT, "longitude": LON,
                             "past_days": past_days, "forecast_days": 1,
                             "hourly": HOURLY, "timezone": "UTC"})
    r.raise_for_status()
    return _to_df(r.json()["hourly"])


def get_weather_full(start: str, end: str) -> pd.DataFrame:
    """Архів + хвіст із forecast-API; на перетині перевага в архіву."""
    arch = get_weather(start, end).dropna(subset=["out_temp"])
    recent = get_weather_recent()
    limit = pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)
    recent = recent[(recent.ts > arch.ts.max()) & (recent.ts <= limit)]
    out = pd.concat([arch, recent], ignore_index=True).drop_duplicates("ts").sort_values("ts")
    return out.reset_index(drop=True)
