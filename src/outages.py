"""Модуль M2 — хроніка блекаутів (проєкт «4331 година»).

Питання: **коли і скільки не було світла?**

Усі датчики квартири живляться від розетки — крім датчика квітки
(`sensor.tiny_soil_sensor_temperature`, батарейка). Сервер Home Assistant
стоїть на ДБЖ. Звідси два різні сигнали:

| Сигнал | Що видно в даних | Точність | Глибина |
|---|---|---|---|
| **A. Коротке відключення** | розеткові датчики замовкли, датчик квітки пише далі | секунди | 10 днів |
| **B. Довге відключення** | сервер теж вимкнувся → в даних немає нічого | 1 година | 6 місяців |

Правило Влада: дірка < 3 хвилин — шум; дірка ≥ 3 хвилин — відключення.

Два уточнення, без яких правило дає хибні спрацювання:

1. **Пристрій, а не entity.** Температура й вологість однієї кімнати — це
   один фізичний датчик: вони зникають разом. Тому «замовк» рахуємо по
   пристрою: пристрій живий, поки пише *хоч одна* його сутність
   (для житлової кімнати це ще й освітленість).
2. **Застряглий датчик ≠ блекаут.** Години з прапорцем `stuck` з M1 не
   рахуємо за наявні, але й за «немає світла» теж — датчик просто не
   є свідком.

Запуск: `PYTHONUTF8=1 python -m src.outages`

Результати:
  * `results/m2_outages.csv`  — таблиця подій (контракт для M4, M8, M9);
  * `results/m2_outages.json` — метрики;
  * `images/m2_*.png`         — чотири графіки.
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns

from .cleaning import KYIV, ROOM_COLORS

# ── Довідники ─────────────────────────────────────────────────────────────────

#: Правило Влада: дірка ≥ 3 хвилин — це відключення, менша — шум.
MIN_OUTAGE = pd.Timedelta(minutes=3)

#: Скільки пристроїв мають замовкнути одночасно, щоб це була подія (а не збій датчика).
MIN_DEVICES = 2

#: Контрольний датчик на батарейці — єдиний, хто переживає відключення.
SOIL = "sensor.tiny_soil_sensor_temperature"

#: Типовий інтервал між записами датчика квітки (він пише рідко!).
SOIL_PERIOD = pd.Timedelta(minutes=20)

#: Розеткові сутності 10-денної історії → фізичний пристрій.
#: Кухня теж тут: у детальних даних вона повна, це найкращий контроль.
DETAILED_DEVICES: dict[str, str] = {
    "sensor.test_product_temperature": "Bedroom",
    "sensor.test_product_humidity": "Bedroom",
    "sensor.test_product_illuminance": "Bedroom",
    "sensor.test_product_temperature_2": "Kitchen",
    "sensor.test_product_humidity_2": "Kitchen",
    "sensor.test_product_temperature_3": "Printernya",
    "sensor.test_product_humidity_3": "Printernya",
}

#: Розеткові датчики погодинних даних (кухня свідомо не входить:
#: у неї власні 30-денні дірки, вона зробила б хибні спрацювання).
HOURLY_MAINS: dict[str, str] = {
    "sensor.test_product_temperature": "bedroom_temp",
    "sensor.test_product_temperature_3": "printernya_temp",
    "sensor.192_168_0_153_cpu_load": "server_cpu_load",
    "sensor.nas_disk_sda_temperature": "nas_sda_temp",
}

#: Дні тижня: у CSV — короткі англійські (стабільні для SQL і Excel),
#: на графіках — українські.
WEEKDAYS_EN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEKDAYS_UA = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]

#: Порядок колонок m2_outages.csv — контракт для M4, M8, M9. Не міняти.
EVENT_COLS = ["event_id", "source", "start_utc", "end_utc", "start_kyiv", "end_kyiv",
              "hours", "minutes", "weekday_kyiv", "start_hour_kyiv", "soil_alive", "notes"]

UPS_NOTE = ("Сервер Home Assistant стоїть на ДБЖ APC Back-UPS BX750MI. "
            "Поки тримає батарея, HA пише далі — тож найкоротші відключення "
            "видно лише по мовчанню розеткових датчиків, а не по зупинці запису. "
            "Коли батарея сідає, сервер гасне і в даних не лишається нічого: "
            "довге відключення видно як порожнечу.")

OUT_COLOR = "#C73E1D"
OK_COLOR = "#2E86AB"


# ── A. Короткі відключення: детальні дані (10 днів) ───────────────────────────
def find_gaps_detailed(ts, min_gap: pd.Timedelta = MIN_OUTAGE) -> pd.DataFrame:
    """Дірки між сусідніми записами одного датчика (фрагмент 14.5 специфікації).

    Це той самий крок, що в SQL робить `LAG(ts) OVER (ORDER BY ts)`.

    Повертає `start` (останній запис перед тишею), `end` (перший запис після),
    `minutes`.
    """
    ts = pd.Series(pd.to_datetime(ts)).sort_values().drop_duplicates().reset_index(drop=True)
    if len(ts) < 2:
        return pd.DataFrame(columns=["start", "end", "minutes"])
    mask = ts.diff() > min_gap
    ev = pd.DataFrame({"start": ts.shift()[mask].reset_index(drop=True),
                       "end": ts[mask].reset_index(drop=True)})
    ev["minutes"] = (ev["end"] - ev["start"]).dt.total_seconds() / 60
    return ev.reset_index(drop=True)


def device_silences(detailed: pd.DataFrame, min_gap: pd.Timedelta = MIN_OUTAGE) -> pd.DataFrame:
    """Періоди тиші кожного *пристрою*: пристрій живий, поки пише хоч одна його сутність.

    Повертає `device`, `start`, `end`, `minutes`.
    """
    d = detailed.copy()
    d["device"] = d["entity_id"].map(DETAILED_DEVICES)
    rows = []
    for dev, g in d[d["device"].notna()].groupby("device", sort=True):
        gaps = find_gaps_detailed(g["ts"], min_gap)
        gaps.insert(0, "device", dev)
        rows.append(gaps)
    out = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(
        columns=["device", "start", "end", "minutes"])
    return out.sort_values("start").reset_index(drop=True)


def merge_silences(sil: pd.DataFrame, min_devices: int = MIN_DEVICES,
                   min_gap: pd.Timedelta = MIN_OUTAGE) -> pd.DataFrame:
    """Склеює тишу різних пристроїв в одну подію.

    Подія — проміжок, у якому одночасно мовчать ≥ `min_devices` пристроїв
    і який триває ≥ `min_gap`. Сусідні такі проміжки об'єднуються.
    """
    empty = pd.DataFrame(columns=["start", "end", "minutes", "devices", "n_devices"])
    if sil.empty:
        return empty
    pts = pd.Series(pd.concat([sil["start"], sil["end"]]).unique()).sort_values().to_list()
    pieces = []
    for a, b in zip(pts[:-1], pts[1:]):
        hit = sil[(sil["start"] <= a) & (sil["end"] >= b)]
        if hit["device"].nunique() >= min_devices:
            pieces.append({"start": a, "end": b, "devices": sorted(hit["device"].unique())})
    if not pieces:
        return empty
    merged = [pieces[0]]
    for p in pieces[1:]:
        if p["start"] <= merged[-1]["end"]:
            merged[-1]["end"] = p["end"]
            merged[-1]["devices"] = sorted(set(merged[-1]["devices"]) | set(p["devices"]))
        else:
            merged.append(p)
    ev = pd.DataFrame(merged)
    ev["minutes"] = (ev["end"] - ev["start"]).dt.total_seconds() / 60
    ev["n_devices"] = ev["devices"].map(len)
    ev["devices"] = ev["devices"].map(", ".join)
    ev = ev[ev["minutes"] >= min_gap.total_seconds() / 60]
    return ev[["start", "end", "minutes", "devices", "n_devices"]].reset_index(drop=True)


def soil_check(detailed: pd.DataFrame, start, end) -> tuple[object, str]:
    """Контроль батарейним датчиком: чи писала квітка під час події?

    Повертає `(soil_alive, пояснення)`. `soil_alive=True` — світла не було,
    але сервер жив на ДБЖ. `False` — сервер теж стояв **або** квітка просто
    не мала запланованого запису: вона пише раз на ~20 хвилин, тому для
    коротких подій контроль нічого не доводить (тоді `pd.NA`).
    """
    s = detailed.loc[detailed["entity_id"] == SOIL, "ts"].sort_values()
    if s.empty:
        return pd.NA, "квітка не писала в цей період узагалі — контроль неможливий"
    inside = s[(s >= start) & (s <= end)]
    if len(inside):
        return True, f"квітка писала {len(inside)} раз(и) під час події → світла не було, сервер жив на ДБЖ"
    if (end - start) < SOIL_PERIOD:
        return pd.NA, (f"квітка мовчала, але подія коротша за її крок (~{SOIL_PERIOD.seconds // 60} хв) — "
                       "контроль нічого не доводить")
    return False, "квітка теж мовчала → або сервер стояв, або датчик був поза мережею"


def detect_short_outages(detailed: pd.DataFrame,
                         min_gap: pd.Timedelta = MIN_OUTAGE) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Сигнал A: кандидати з 10-денної історії + перевірка.

    Повертає `(confirmed, candidates)`. Кандидат стає підтвердженим, тільки
    якщо замовкли **всі** розеткові пристрої: якщо хоч один писав далі —
    світло було, а це збій датчика.
    """
    sil = device_silences(detailed, min_gap)
    cand = merge_silences(sil, MIN_DEVICES, min_gap)
    n_dev_total = detailed["entity_id"].map(DETAILED_DEVICES).nunique()
    if cand.empty:
        cand = cand.assign(soil_alive=pd.Series(dtype="object"), verdict=pd.Series(dtype="str"),
                           notes=pd.Series(dtype="str"))
        return cand, cand
    soil, note, verdict = [], [], []
    for r in cand.itertuples():
        alive, why = soil_check(detailed, r.start, r.end)
        ok = r.n_devices >= n_dev_total
        soil.append(alive)
        verdict.append("підтверджено" if ok else "відхилено")
        note.append(f"мовчать {r.devices} ({r.n_devices} з {n_dev_total}); {why}"
                    if ok else
                    f"мовчать лише {r.devices} ({r.n_devices} з {n_dev_total}) — решта писала далі, "
                    f"отже світло було: збій датчика, не блекаут")
    cand = cand.assign(soil_alive=soil, verdict=verdict, notes=note)
    return cand[cand["verdict"] == "підтверджено"].reset_index(drop=True), cand


# ── B. Довгі відключення: погодинні дані (6 місяців) ──────────────────────────
def mains_alive_matrix(hourly: pd.DataFrame) -> pd.DataFrame:
    """Матриця «датчик живий у цю годину» по розеткових датчиках.

    `True` — є запис і він не в серії `stuck`; `False` — немає;
    `NA` — датчик ще не встановлений або застряг (не свідок).
    """
    sub = hourly[hourly["entity_id"].isin(HOURLY_MAINS)].copy()
    cal = pd.date_range(hourly["ts"].min(), hourly["ts"].max(), freq="h", tz="UTC")
    out = pd.DataFrame(index=cal)
    for eid, name in HOURLY_MAINS.items():
        g = sub[sub["entity_id"] == eid].set_index("ts")
        if g.empty:
            out[name] = pd.Series(pd.NA, index=cal, dtype="object")
            continue
        has = g["mean"].notna().reindex(cal, fill_value=False)
        stuck = g["stuck"].reindex(cal, fill_value=False).astype(bool)
        col = pd.Series(has.to_numpy(), index=cal, dtype="object")
        first = g.index[g["mean"].notna()].min()
        col[cal < first] = pd.NA        # ще не встановлений
        col[stuck.to_numpy()] = pd.NA   # застряг — не свідок
        out[name] = col
    return out


def find_outages_hourly(hourly: pd.DataFrame) -> pd.DataFrame:
    """Сигнал B: години, коли мовчать **усі** доступні розеткові датчики.

    Сусідні такі години склеюються в подію. `end` — година, коли дані
    повернулися (тобто інтервал `[start, end)`), `hours` — скільки годин без світла.
    """
    m = mains_alive_matrix(hourly)
    witnesses = m.notna().sum(axis=1)
    alive = m.eq(True).sum(axis=1)
    off = (witnesses > 0) & (alive == 0)
    idx = off[off].index
    cols = ["start", "end", "hours", "n_witnesses", "witnesses"]
    if len(idx) == 0:
        return pd.DataFrame(columns=cols)
    brk = np.r_[True, np.diff(idx.to_numpy()).astype("timedelta64[h]").astype(int) != 1]
    grp = np.cumsum(brk)
    rows = []
    for g in np.unique(grp):
        hrs = idx[grp == g]
        w = m.loc[hrs].notna().any(axis=0)
        rows.append({"start": hrs.min(), "end": hrs.max() + pd.Timedelta(hours=1),
                     "hours": len(hrs), "n_witnesses": int(w.sum()),
                     "witnesses": ", ".join(w[w].index)})
    return pd.DataFrame(rows, columns=cols)


def hourly_notes(ev: pd.Series, hourly: pd.DataFrame) -> str:
    """Короткий коментар до довгої події: скільки свідків і чи мовчала квітка."""
    parts = [f"мовчать усі доступні розеткові датчики ({ev['n_witnesses']}): {ev['witnesses']}"]
    if ev["n_witnesses"] < 2:
        parts.append("НИЗЬКА ВПЕВНЕНІСТЬ: у той час працював лише один розетковий датчик")
    soil = hourly[(hourly["entity_id"] == SOIL) & hourly["mean"].notna()]
    if not soil.empty and soil["ts"].min() <= ev["start"]:
        wrote = ((soil["ts"] >= ev["start"]) & (soil["ts"] < ev["end"])).sum()
        parts.append("квітка теж мовчала → сервер був вимкнений" if wrote == 0
                     else f"квітка писала {wrote} год → сервер жив на ДБЖ")
    return "; ".join(parts)


# ── C. Єдина таблиця подій ────────────────────────────────────────────────────
def build_event_table(short_ev: pd.DataFrame, long_ev: pd.DataFrame,
                      hourly: pd.DataFrame) -> pd.DataFrame:
    """Зводить обидва сигнали в одну таблицю подій (контракт `results/m2_outages.csv`).

    Дублікати (одна подія видна обома способами) прибираємо на користь
    детальних даних — вони точніші.
    """
    rows = []
    for r in short_ev.itertuples():
        rows.append({"source": "detailed", "start_utc": r.start, "end_utc": r.end,
                     "soil_alive": r.soil_alive, "notes": r.notes})
    for _, r in long_ev.iterrows():
        rows.append({"source": "hourly", "start_utc": r["start"], "end_utc": r["end"],
                     "soil_alive": pd.NA, "notes": hourly_notes(r, hourly)})
    ev = pd.DataFrame(rows, columns=["source", "start_utc", "end_utc", "soil_alive", "notes"])
    if ev.empty:
        return pd.DataFrame(columns=EVENT_COLS)

    # дедуплікація: погодинна подія, що перетинається з детальною, — та сама подія
    det = ev[ev["source"] == "detailed"]
    drop = []
    for i, r in ev[ev["source"] == "hourly"].iterrows():
        hit = det[(det["start_utc"] < r["end_utc"]) & (det["end_utc"] > r["start_utc"])]
        if not hit.empty:
            drop.append(i)
    ev = ev.drop(index=drop).sort_values("start_utc").reset_index(drop=True)

    ev["start_kyiv"] = ev["start_utc"].dt.tz_convert(KYIV)
    ev["end_kyiv"] = ev["end_utc"].dt.tz_convert(KYIV)
    dur = ev["end_utc"] - ev["start_utc"]
    ev["hours"] = (dur.dt.total_seconds() / 3600).round(3)
    ev["minutes"] = (dur.dt.total_seconds() / 60).round(1)
    ev["weekday_kyiv"] = ev["start_kyiv"].dt.dayofweek.map(dict(enumerate(WEEKDAYS_EN)))
    ev["start_hour_kyiv"] = ev["start_kyiv"].dt.hour
    ev["event_id"] = [f"OUT-{i:03d}" for i in range(1, len(ev) + 1)]
    return ev[EVENT_COLS]


# ── D. Метрики ────────────────────────────────────────────────────────────────
def off_hours_calendar(events: pd.DataFrame) -> pd.DataFrame:
    """Розгортає події в окремі години без світла (київський час) — основа теплової карти."""
    rows = []
    for r in events.itertuples():
        for t in pd.date_range(r.start_utc, r.end_utc, freq="h", inclusive="left"):
            k = t.tz_convert(KYIV)
            rows.append({"ts": t, "ts_kyiv": k, "hour": k.hour, "weekday": k.dayofweek,
                         "month": k.strftime("%Y_%m"), "event_id": r.event_id})
    return pd.DataFrame(rows, columns=["ts", "ts_kyiv", "hour", "weekday", "month", "event_id"])


def outage_metrics(events: pd.DataFrame, hourly: pd.DataFrame,
                   candidates: pd.DataFrame, silences: pd.DataFrame) -> dict:
    """Плаский словник метрик для `results/m2_outages.json`."""
    cal_start, cal_end = hourly["ts"].min(), hourly["ts"].max() + pd.Timedelta(hours=1)
    total_cal = (cal_end - cal_start).total_seconds() / 3600
    total_off = float(events["hours"].sum()) if len(events) else 0.0
    oh = off_hours_calendar(events)

    m: dict = {
        "n_events_total": int(len(events)),
        "n_short": int((events["hours"] < 1).sum()) if len(events) else 0,
        "n_long": int((events["hours"] >= 1).sum()) if len(events) else 0,
        "median_hours": round(float(events["hours"].median()), 2) if len(events) else 0.0,
        "max_hours": round(float(events["hours"].max()), 2) if len(events) else 0.0,
        "max_event_date": (events.loc[events["hours"].idxmax(), "start_kyiv"].strftime("%Y-%m-%d")
                           if len(events) else ""),
        "total_hours": round(total_off, 2),
        "uptime_pct": round(100 * (1 - total_off / total_cal), 3),
        "min_gap_minutes": int(MIN_OUTAGE.total_seconds() // 60),
        "ups_note": UPS_NOTE,
    }

    months = pd.period_range(cal_start.tz_convert(KYIV), cal_end.tz_convert(KYIV), freq="M")
    by_month = oh.groupby("month").size() if len(oh) else pd.Series(dtype=int)
    for p in months:
        key = f"{p.year}_{p.month:02d}"
        m[f"hours_{key}"] = int(by_month.get(key, 0))

    by_wd_ev = events["weekday_kyiv"].value_counts() if len(events) else pd.Series(dtype=int)
    by_wd_h = oh.groupby("weekday").size() if len(oh) else pd.Series(dtype=int)
    for i, name in enumerate(WEEKDAYS_EN):
        m[f"events_weekday_{name.lower()}"] = int(by_wd_ev.get(name, 0))
        m[f"hours_weekday_{name.lower()}"] = int(by_wd_h.get(i, 0))

    m.update({
        "period_start_utc": str(cal_start),
        "period_end_utc": str(cal_end),
        "total_calendar_hours": int(round(total_cal)),
        "n_events_detailed": int((events["source"] == "detailed").sum()) if len(events) else 0,
        "n_events_hourly": int((events["source"] == "hourly").sum()) if len(events) else 0,
        "n_detailed_candidates": int(len(candidates)),
        "n_detailed_rejected": int((candidates["verdict"] == "відхилено").sum()) if len(candidates) else 0,
        "n_device_silences_10d": int(len(silences)),
        "n_low_confidence": int(events["notes"].str.contains("НИЗЬКА").sum()) if len(events) else 0,
        "longest_event_start_kyiv": (events.loc[events["hours"].idxmax(), "start_kyiv"].strftime("%Y-%m-%d %H:%M")
                                     if len(events) else ""),
    })
    return m


# ── E. Графіки ────────────────────────────────────────────────────────────────
def _style() -> None:
    sns.set_theme(style="whitegrid")
    plt.rcParams.update({"font.size": 12, "axes.titlesize": 14, "axes.labelsize": 12})


def _kyiv(x):
    """Київський час без мітки поясу — matplotlib малює осі в UTC, тож переводимо самі."""
    if isinstance(x, pd.Series):
        return x.dt.tz_convert(KYIV).dt.tz_localize(None)
    return pd.Timestamp(x).tz_convert(KYIV).tz_localize(None)


def _save(fig, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


def plot_timeline(events: pd.DataFrame, hourly: pd.DataFrame, out: Path) -> Path:
    """Стрічка часу: коли сталося відключення (вісь X) і на скільки годин (висота)."""
    _style()
    fig, ax = plt.subplots(figsize=(10, 4.5))
    t0 = _kyiv(hourly["ts"].min())
    t1 = _kyiv(hourly["ts"].max() + pd.Timedelta(hours=1))
    ax.axhline(0, color="#ADB5BD", lw=1)
    for r in events.itertuples():
        color = OUT_COLOR if r.hours >= 1 else OK_COLOR
        x = _kyiv(r.start_utc)
        ax.vlines(x, 0, r.hours, color=color, lw=6, alpha=.9)
        ax.text(x, r.hours + .35, f"{r.hours:.0f} год\n{x:%d.%m}",
                ha="center", va="bottom", fontsize=10)
    ax.set_xlim(t0, t1)
    ax.set_ylim(0, max(events["hours"].max() * 1.45, 4) if len(events) else 4)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b"))
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.set_title("Відключення світла за 6 місяців: коли і на скільки")
    ax.set_xlabel("місяць (київський час)")
    ax.set_ylabel("тривалість, годин")
    return _save(fig, out)


def plot_heatmap(events: pd.DataFrame, out: Path) -> Path:
    """Теплова карта: години без світла по годинах доби і днях тижня (київський час)."""
    _style()
    oh = off_hours_calendar(events)
    grid = pd.DataFrame(0, index=range(7), columns=range(24))
    if len(oh):
        c = oh.groupby(["weekday", "hour"]).size()
        for (w, h), n in c.items():
            grid.loc[w, h] = n
    fig, ax = plt.subplots(figsize=(10, 5))
    labels = grid.map(lambda v: str(int(v)) if v else "")
    sns.heatmap(grid, cmap="Reds", annot=labels, fmt="", linewidths=.5,
                linecolor="white", cbar_kws={"label": "годин без світла"}, ax=ax)
    ax.set_yticklabels(WEEKDAYS_UA, rotation=0)
    ax.set_xlabel("година доби (Київ)")
    ax.set_ylabel("")
    ax.set_title("Коли зникало світло: години доби × дні тижня")
    return _save(fig, out)


def plot_monthly(events: pd.DataFrame, hourly: pd.DataFrame, out: Path) -> Path:
    """Стовпчики: скільки годин без світла припало на кожен місяць."""
    _style()
    oh = off_hours_calendar(events)
    cal_start = hourly["ts"].min().tz_convert(KYIV)
    cal_end = (hourly["ts"].max() + pd.Timedelta(hours=1)).tz_convert(KYIV)
    months = pd.period_range(cal_start, cal_end, freq="M")
    vals = [int(oh[oh["month"] == f"{p.year}_{p.month:02d}"].shape[0]) if len(oh) else 0
            for p in months]
    names_ua = {3: "берез.", 4: "квіт.", 5: "трав.", 6: "черв.", 7: "лип.", 8: "серп.", 9: "верес."}
    labels = [names_ua.get(p.month, f"{p.month:02d}") for p in months]
    fig, ax = plt.subplots(figsize=(10, 4.5))
    bars = ax.bar(labels, vals, color=[OUT_COLOR if v else "#DEE2E6" for v in vals])
    for b, v in zip(bars, vals):
        ax.text(b.get_x() + b.get_width() / 2, v + .3, str(v), ha="center", fontsize=11)
    ax.set_ylim(0, max(max(vals) * 1.25, 3))
    ax.set_title("Годин без світла за місяць")
    ax.set_ylabel("годин")
    ax.set_xlabel("")
    return _save(fig, out)


FOCUS = "sensor.test_product_temperature"   # приклад малюємо по датчику житлової кімнати


def plot_short_example(detailed: pd.DataFrame, out: Path,
                       start=None, end=None) -> Path:
    """«Як ми бачимо відключення»: один розетковий датчик замовк, інші пишуть далі.

    Беремо найдовшу тишу датчика житлової кімнати з 10-денних даних (без
    багатодобового збою пристрою) і показуємо поруч кухню й батарейну квітку.
    """
    _style()
    if start is None:
        g = find_gaps_detailed(detailed.loc[detailed["entity_id"] == FOCUS, "ts"])
        g = g[g["minutes"] < 12 * 60].sort_values("minutes", ascending=False)
        row = g.iloc[0]
        start, end = row["start"], row["end"]
    pad = pd.Timedelta(minutes=60)
    w = detailed[(detailed["ts"] >= start - pad) & (detailed["ts"] <= end + pad)]
    series = [(FOCUS, "житлова кімната (розетка)", ROOM_COLORS["Bedroom"]),
              ("sensor.test_product_temperature_2", "кухня (розетка)", ROOM_COLORS["Kitchen"]),
              (SOIL, "квітка (батарейка)", ROOM_COLORS["Soil"])]
    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.axvspan(_kyiv(start), _kyiv(end), color=OUT_COLOR, alpha=.12)
    for eid, label, color in series:
        g = w[w["entity_id"] == eid].sort_values("ts")
        if g.empty:
            continue
        x, y = _kyiv(g["ts"]), g["value"]
        if eid == SOIL:
            ax.plot(x, y, color=color, label=label, marker="o", ms=4, ls="none")
            continue
        y = y.mask(x.diff() > MIN_OUTAGE)   # рвемо лінію на дірці, не з'єднуємо через тишу
        ax.plot(x, y, color=color, label=label, lw=2)
    mins = (end - start).total_seconds() / 60
    ax.text(_kyiv(start) + (end - start) / 2, ax.get_ylim()[1], f"тиша {mins:.0f} хв",
            ha="center", va="top", fontsize=11, color=OUT_COLOR)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.set_title(f"Тиша розеткового датчика {_kyiv(start):%d.%m}: "
                 "сусіди пишуть далі — значить, світло було")
    ax.set_xlabel("київський час")
    ax.set_ylabel("температура, °C")
    ax.legend(loc="lower right", fontsize=10)
    return _save(fig, out)


# ── Запуск усього модуля ──────────────────────────────────────────────────────
def load_clean(root: str | Path = ".") -> tuple[pd.DataFrame, pd.DataFrame]:
    """Читає `data/clean/readings_hourly.csv` і `readings_detailed.csv`."""
    clean = Path(root) / "data" / "clean"
    hourly = pd.read_csv(clean / "readings_hourly.csv")
    hourly["ts"] = pd.to_datetime(hourly["ts"], utc=True, format="mixed")
    hourly["stuck"] = hourly["stuck"].astype(bool)
    detailed = pd.read_csv(clean / "readings_detailed.csv")
    detailed["ts"] = pd.to_datetime(detailed["ts"], utc=True, format="mixed")
    return hourly, detailed


def run_all(root: str | Path = ".") -> dict:
    """Повний прогін M2: таблиця подій, метрики, чотири графіки."""
    root = Path(root)
    hourly, detailed = load_clean(root)

    silences = device_silences(detailed)
    short_ev, candidates = detect_short_outages(detailed)
    long_ev = find_outages_hourly(hourly)
    events = build_event_table(short_ev, long_ev, hourly)
    metrics = outage_metrics(events, hourly, candidates, silences)

    (root / "results").mkdir(exist_ok=True)
    events.to_csv(root / "results" / "m2_outages.csv", index=False)
    (root / "results" / "m2_outages.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")

    img = root / "images"
    plot_timeline(events, hourly, img / "m2_timeline.png")
    plot_heatmap(events, img / "m2_heatmap_hour_weekday.png")
    plot_monthly(events, hourly, img / "m2_monthly_hours.png")
    plot_short_example(detailed, img / "m2_short_outage_example.png")
    return metrics


def main() -> None:
    matplotlib.use("Agg")
    m = run_all(Path(__file__).resolve().parents[1])
    print(f"Подій: {m['n_events_total']} | годин без світла: {m['total_hours']:.0f} | "
          f"найдовша: {m['max_hours']:.0f} год ({m['max_event_date']}) | "
          f"uptime: {m['uptime_pct']:.2f} %")


if __name__ == "__main__":
    main()
