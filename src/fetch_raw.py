"""Крок 3 плану: вивантажити все в data/raw/.

  python -m src.fetch_raw          (запускати з кореня репозиторію)

Пише:
  data/raw/stats_hourly.csv  — погодинна статистика всіх датчиків + техніки (6 міс.)
  data/raw/history_10d.csv   — детальна історія датчиків (10 днів)
  data/raw/weather.csv       — погода Харкова, UTC
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from src import ha_client as ha
from src import weather as wx

RAW = Path(__file__).resolve().parents[1] / "data" / "raw"
RAW.mkdir(parents=True, exist_ok=True)


def main() -> None:
    assert ha.ping(), "Home Assistant не відповідає"
    ids = list(ha.SENSORS) + list(ha.TECH)

    print("1/3 статистика (WebSocket)…")
    st = ha.get_statistics(ids, days=400)
    st.to_csv(RAW / "stats_hourly.csv", index=False)
    print(f"    {len(st):,} рядків, {st.ts.min()} → {st.ts.max()}")

    print("2/3 історія 10 днів (REST)…")
    hi = ha.get_history(list(ha.SENSORS), days=10)
    hi.to_csv(RAW / "history_10d.csv", index=False)
    print(f"    {len(hi):,} рядків, {hi.ts.min()} → {hi.ts.max()}")

    print("3/3 погода Open-Meteo…")
    start = st.ts.min().strftime("%Y-%m-%d")
    end = dt.date.today().strftime("%Y-%m-%d")
    w = wx.get_weather_full(start, end)
    w.to_csv(RAW / "weather.csv", index=False)
    print(f"    {len(w):,} годин, {w.ts.min()} → {w.ts.max()}, NaN temp: {w.out_temp.isna().sum()}")


if __name__ == "__main__":
    main()
