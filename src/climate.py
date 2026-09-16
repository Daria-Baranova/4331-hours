"""Модуль M3 — квартира проти погоди 🌡️ + додатковий показник «ризик цвілі».

Питання модуля: як вулиця впливає на квартиру і з якою затримкою?

Що рахуємо (розділи специфікації «M3» і «Додатковий параметр — ризик цвілі»):
  1. описова статистика по кімнатах і надворі (mean / min / max / std) + таблиця по місяцях;
  2. добовий цикл — середня температура по годині доби (Київ);
  3. згладжування — у скільки разів дім гасить добовий перепад вулиці;
  4. лаг — на скільки годин дім відстає від вулиці (крос-кореляція, зсув 0…24 год);
  5. чутливість — +1 °C надворі = +X °C вдома (OLS, statsmodels);
  6. комфорт — % годин поза 20–24 °C і поза 30–60 % вологості;
  7. ризик цвілі — точка роси (формула Магнуса) + частка годин у «мокрих» серіях
     (вологість ≥ 65 % поспіль ≥ 6 годин).

Дані: `data/clean/hours_wide.csv` (M1). Години з прапорцем `stuck`
(датчик завис — у житловій кімнаті 144 год поспіль 18–24.08) виключаємо:
M1 лишив значення на місці, але для статистики вони — не факт, а артефакт.

Запуск: `PYTHONUTF8=1 python -m src.climate`
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
import statsmodels.api as sm

from .cleaning import KYIV, ROOM_COLORS, ROOM_LABELS_UA

# ── Довідники ─────────────────────────────────────────────────────────────────

#: Кімнати модуля (техніку й датчик квітки тут не аналізуємо).
ROOMS: list[str] = ["Bedroom", "Kitchen", "Printernya"]

#: Кімната -> префікс колонок у широкій таблиці.
ROOM_PREFIX: dict[str, str] = {
    "Bedroom": "bedroom",
    "Kitchen": "kitchen",
    "Printernya": "printernya",
}

#: Колір вулиці на графіках (єдиний для проєкту).
OUT_COLOR: str = "#6C757D"
OUT_LABEL_UA: str = "надворі"

#: Комфортні межі зі специфікації.
COMFORT_TEMP: tuple[float, float] = (20.0, 24.0)
COMFORT_HUM: tuple[float, float] = (30.0, 60.0)

#: Ризик цвілі: вологість ≥ 65 % поспіль ≥ 6 годин.
MOULD_RH: float = 65.0
MOULD_RUN_H: int = 6

#: Формула Магнуса (коефіцієнти зі специфікації).
MAGNUS_A: float = 17.62
MAGNUS_B: float = 243.12

#: Мінімум годин на добу, щоб вважати добу придатною для денного перепаду.
MIN_HOURS_PER_DAY: int = 20

#: Максимальний зсув для крос-кореляції, годин.
MAX_LAG_H: int = 24

MONTHS_UA: dict[int, str] = {
    3: "березень", 4: "квітень", 5: "травень", 6: "червень",
    7: "липень", 8: "серпень", 9: "вересень",
}


def room_ua(room: str) -> str:
    """Українська назва кімнати для підписів."""
    return ROOM_LABELS_UA.get(room, room)


def temp_col(room: str) -> str:
    return f"{ROOM_PREFIX[room]}_temp"


def hum_col(room: str) -> str:
    return f"{ROOM_PREFIX[room]}_hum"


# ── Завантаження ──────────────────────────────────────────────────────────────
def _stuck_mask(root: Path) -> pd.DataFrame:
    """Таблиця «година × колонка» з прапорцем stuck із `readings_hourly.csv`."""
    hourly = pd.read_csv(root / "data" / "clean" / "readings_hourly.csv")
    hourly["ts"] = pd.to_datetime(hourly["ts"], utc=True)
    hourly = hourly[hourly["room"].isin(ROOMS) & hourly["metric"].isin(["temperature", "humidity"])]
    suffix = {"temperature": "_temp", "humidity": "_hum"}
    hourly = hourly.assign(
        col=hourly["room"].map(ROOM_PREFIX) + hourly["metric"].map(suffix)
    )
    return (
        hourly.pivot_table(index="ts", columns="col", values="stuck", aggfunc="max")
        .astype("boolean")
        .fillna(False)
        .astype(bool)
    )


def load_climate(root: str | Path = ".") -> tuple[pd.DataFrame, dict]:
    """Широка погодинна таблиця для M3 (застряглі години → NaN).

    Повертає (df, info), де df має індекс UTC, колонки кімнат і вулиці,
    а також `ts_kyiv`, `hour` (година доби в Києві), `date` (київська доба),
    `month`; info — скільки годин прибрано через stuck.
    """
    root = Path(root)
    df = pd.read_csv(root / "data" / "clean" / "hours_wide.csv")
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.set_index("ts").sort_index()

    stuck = _stuck_mask(root).reindex(df.index).fillna(False).astype(bool)
    removed: dict[str, int] = {}
    for col in stuck.columns:
        if col in df.columns:
            mask = stuck[col] & df[col].notna()
            removed[col] = int(mask.sum())
            df.loc[mask, col] = np.nan

    kyiv = df.index.tz_convert(KYIV)
    df["ts_kyiv"] = kyiv
    df["hour"] = kyiv.hour
    df["date"] = kyiv.date
    df["month"] = kyiv.strftime("%Y-%m")

    info = {
        "rows": int(len(df)),
        "stuck_hours_removed": int(sum(removed.values())),
        "stuck_removed_by_col": removed,
        "ts_start_kyiv": str(kyiv.min()),
        "ts_end_kyiv": str(kyiv.max()),
    }
    return df, info


# ── 1. Описова статистика ─────────────────────────────────────────────────────
def describe_rooms(df: pd.DataFrame) -> pd.DataFrame:
    """mean / min / max / std температури й вологості по кімнатах + надворі."""
    rows = []
    for room in ROOMS:
        for metric, col in (("температура", temp_col(room)), ("вологість", hum_col(room))):
            s = df[col].dropna()
            rows.append({
                "room": room, "room_ua": room_ua(room), "metric": metric,
                "n_hours": int(s.size), "mean": s.mean(), "min": s.min(),
                "max": s.max(), "std": s.std(),
            })
    for metric, col in (("температура", "out_temp"), ("вологість", "out_hum")):
        s = df[col].dropna()
        rows.append({
            "room": "Outdoor", "room_ua": OUT_LABEL_UA, "metric": metric,
            "n_hours": int(s.size), "mean": s.mean(), "min": s.min(),
            "max": s.max(), "std": s.std(),
        })
    return pd.DataFrame(rows).round(2)


def monthly_table(df: pd.DataFrame) -> pd.DataFrame:
    """Місяць × кімната: середня / мін / макс температура і середня вологість."""
    rows = []
    for month, chunk in df.groupby("month", sort=True):
        for room in ROOMS + ["Outdoor"]:
            tcol = "out_temp" if room == "Outdoor" else temp_col(room)
            hcol = "out_hum" if room == "Outdoor" else hum_col(room)
            t, h = chunk[tcol].dropna(), chunk[hcol].dropna()
            if t.empty and h.empty:
                continue
            rows.append({
                "month": month,
                "room": room,
                "room_ua": OUT_LABEL_UA if room == "Outdoor" else room_ua(room),
                "n_hours": int(t.size),
                "temp_mean": t.mean(), "temp_min": t.min(), "temp_max": t.max(),
                "temp_std": t.std(), "hum_mean": h.mean(),
            })
    return pd.DataFrame(rows).round(2)


# ── 2. Добовий цикл ───────────────────────────────────────────────────────────
def daily_cycle(df: pd.DataFrame) -> pd.DataFrame:
    """Середня температура по годині доби (київський час): рядок = година."""
    out = pd.DataFrame(index=pd.RangeIndex(24, name="hour"))
    for room in ROOMS:
        out[room] = df.groupby("hour")[temp_col(room)].mean()
    out["Outdoor"] = df.groupby("hour")["out_temp"].mean()
    return out.round(3)


# ── 3. Згладжування ───────────────────────────────────────────────────────────
def daily_ranges(df: pd.DataFrame, col: str, min_hours: int = MIN_HOURS_PER_DAY) -> pd.Series:
    """Денний перепад (max − min) по київських добах із достатнім покриттям."""
    g = df.groupby("date")[col]
    rng = g.max() - g.min()
    return rng[g.count() >= min_hours].dropna()


def smoothing(df: pd.DataFrame) -> pd.DataFrame:
    """Згладжування: середній денний перепад надворі / вдома (спільні доби)."""
    out_rng = daily_ranges(df, "out_temp")
    rows = []
    for room in ROOMS:
        room_rng = daily_ranges(df, temp_col(room))
        common = room_rng.index.intersection(out_rng.index)
        ind, outd = room_rng.loc[common], out_rng.loc[common]
        rows.append({
            "room": room, "room_ua": room_ua(room), "n_days": int(len(common)),
            "indoor_daily_range": ind.mean(), "outdoor_daily_range": outd.mean(),
            "smoothing_x": outd.mean() / ind.mean() if ind.mean() else np.nan,
        })
    return pd.DataFrame(rows).round(2)


# ── 4. Лаг ────────────────────────────────────────────────────────────────────
def _anomaly(s: pd.Series, window: int = 24) -> pd.Series:
    """Відхилення від локального добового середнього (ковзне вікно 24 год).

    Прибирає сезонний хід (за пів року вулиця прогрілася на десятки градусів),
    лишає саме добову хвилю — а її зсув по фазі і є фізичний лаг.
    """
    return s - s.rolling(window, center=True, min_periods=window - 6).mean()


def cross_correlation(df: pd.DataFrame, room: str, max_lag: int = MAX_LAG_H,
                      method: str = "anomaly") -> pd.Series:
    """Кореляція «вулиця зі зсувом k годин» vs кімната, k = 0…max_lag."""
    out, ind = df["out_temp"], df[temp_col(room)]
    if method == "anomaly":
        out, ind = _anomaly(out), _anomaly(ind)
    elif method == "diff":
        out, ind = out.diff(), ind.diff()
    else:  # pragma: no cover - захист від друкарської помилки
        raise ValueError(f"невідомий метод: {method}")
    corr = {k: out.shift(k).corr(ind) for k in range(max_lag + 1)}
    return pd.Series(corr, name=f"{room}_{method}").rename_axis("lag_h")


#: Нижче цієї кореляції пік — шум, і лаг вважаємо невизначеним.
LAG_MIN_CORR: float = 0.20


def _peak_lag(corr: pd.Series) -> tuple[int, float]:
    """Пік крос-кореляції у фізичних годинах.

    Крива майже періодична з періодом 24 год (добова хвиля), тому зсуви 0 і 24
    означають одне й те саме. Зсув k > 12 читаємо як k − 24: від'ємний лаг —
    кімната випереджає вулицю. Беремо найкращий пік серед унікальних
    фізичних лагів у діапазоні (−12; +12].
    """
    best_lag, best_corr = 0, -np.inf
    for k, c in corr.items():
        if np.isnan(c):
            continue
        phys = int(k) - 24 if int(k) > 12 else int(k)
        if c > best_corr:
            best_lag, best_corr = phys, float(c)
    return best_lag, best_corr


def lags(df: pd.DataFrame, max_lag: int = MAX_LAG_H) -> pd.DataFrame:
    """Лаг = пік крос-кореляції; рахуємо двома способами для перевірки."""
    rows = []
    for room in ROOMS:
        anom = cross_correlation(df, room, max_lag, "anomaly")
        diff = cross_correlation(df, room, max_lag, "diff")
        lag_a, corr_a = _peak_lag(anom)
        lag_d, corr_d = _peak_lag(diff)
        rows.append({
            "room": room, "room_ua": room_ua(room),
            "lag_h": lag_a, "corr_at_lag": corr_a, "corr_at_0": float(anom.loc[0]),
            "lag_identifiable": bool(corr_a >= LAG_MIN_CORR),
            "lag_h_diff": lag_d, "corr_diff_at_lag": corr_d,
        })
    return pd.DataFrame(rows).round(3)


# ── 5. Чутливість ─────────────────────────────────────────────────────────────
def sensitivity(df: pd.DataFrame, room: str, daily: bool = False) -> dict:
    """OLS: температура в кімнаті ~ температура надворі.

    `daily=False` — погодинні дані з поправкою Ньюї–Веста на автокореляцію
    (HAC, maxlags=24); `daily=True` — середні по добі (спостереження майже
    незалежні, тому довірчий інтервал чесніший).
    """
    if daily:
        data = df.groupby("date")[["out_temp", temp_col(room)]].mean()
        x, y = data["out_temp"], data[temp_col(room)]
        fit_kw = {}
    else:
        x, y = df["out_temp"], df[temp_col(room)]
        fit_kw = {"cov_type": "HAC", "cov_kwds": {"maxlags": 24}}
    ok = x.notna() & y.notna()
    model = sm.OLS(y[ok].to_numpy(), sm.add_constant(x[ok].to_numpy())).fit(**fit_kw)
    lo, hi = model.conf_int()[1]
    return {
        "room": room, "room_ua": room_ua(room), "scope": "daily" if daily else "hourly",
        "n": int(ok.sum()), "slope": float(model.params[1]),
        "ci_low": float(lo), "ci_high": float(hi),
        "r2": float(model.rsquared), "intercept": float(model.params[0]),
        "p_value": float(model.pvalues[1]),
    }


def sensitivity_table(df: pd.DataFrame) -> pd.DataFrame:
    rows = [sensitivity(df, r, daily=d) for r in ROOMS for d in (False, True)]
    return pd.DataFrame(rows).round(4)


# ── 6. Комфорт ────────────────────────────────────────────────────────────────
def _share(s: pd.Series, lo: float, hi: float) -> tuple[float, float, float, int]:
    s = s.dropna()
    n = s.size
    if n == 0:
        return np.nan, np.nan, np.nan, 0
    return (s < lo).sum() / n * 100, ((s >= lo) & (s <= hi)).sum() / n * 100, \
        (s > hi).sum() / n * 100, int(n)


def comfort(df: pd.DataFrame, by_month: bool = False) -> pd.DataFrame:
    """% годин нижче / всередині / вище комфортних меж (20–24 °C, 30–60 %)."""
    groups = df.groupby("month", sort=True) if by_month else [("усе", df)]
    rows = []
    for key, chunk in groups:
        for room in ROOMS:
            t_lo, t_ok, t_hi, n_t = _share(chunk[temp_col(room)], *COMFORT_TEMP)
            h_lo, h_ok, h_hi, n_h = _share(chunk[hum_col(room)], *COMFORT_HUM)
            if n_t == 0 and n_h == 0:
                continue
            rows.append({
                "month": key, "room": room, "room_ua": room_ua(room),
                "n_hours_temp": n_t, "too_cold_pct": t_lo,
                "temp_ok_pct": t_ok, "too_hot_pct": t_hi,
                "n_hours_hum": n_h, "too_dry_pct": h_lo,
                "hum_ok_pct": h_ok, "too_humid_pct": h_hi,
            })
    out = pd.DataFrame(rows).round(1)
    return out if by_month else out.drop(columns="month")


# ── 7. Ризик цвілі ────────────────────────────────────────────────────────────
def dew_point(temp_c: pd.Series | float, rh_pct: pd.Series | float,
              a: float = MAGNUS_A, b: float = MAGNUS_B) -> pd.Series | float:
    """Точка роси, °C — формула Магнуса (a = 17.62, b = 243.12 °C)."""
    rh = np.clip(rh_pct, 1e-6, 100.0)
    gamma = np.log(rh / 100.0) + (a * temp_c) / (b + temp_c)
    return (b * gamma) / (a - gamma)


def _wet_runs(flag: pd.Series, min_len: int = MOULD_RUN_H) -> pd.Series:
    """Булева маска годин, що входять у серію ≥ min_len поспіль True.

    NaN (немає даних) розриває серію — не домальовуємо того, чого не бачили.
    """
    val = flag.fillna(False).to_numpy(dtype=bool)
    out = np.zeros(val.size, dtype=bool)
    start = None
    for i, v in enumerate(val):
        if v and start is None:
            start = i
        elif not v and start is not None:
            if i - start >= min_len:
                out[start:i] = True
            start = None
    if start is not None and val.size - start >= min_len:
        out[start:] = True
    return pd.Series(out, index=flag.index)


def mould_risk(df: pd.DataFrame, rh_threshold: float = MOULD_RH,
               min_run: int = MOULD_RUN_H) -> pd.DataFrame:
    """Ризик цвілі: частка годин усередині серій «вологість ≥ поріг» ≥ min_run год."""
    rows = []
    for room in ROOMS:
        hum = df[hum_col(room)]
        valid = hum.notna()
        flag = hum.where(valid) >= rh_threshold
        risk = _wet_runs(flag.where(valid), min_run) & valid
        dp = dew_point(df[temp_col(room)], hum)
        n = int(valid.sum())
        runs, cur = [], 0
        for v in risk.to_numpy():
            if v:
                cur += 1
            elif cur:
                runs.append(cur)
                cur = 0
        if cur:
            runs.append(cur)
        margin = df[temp_col(room)] - dp  # запас до конденсації, °C
        rows.append({
            "room": room, "room_ua": room_ua(room), "n_hours": n,
            "hours_ge_threshold": int((hum >= rh_threshold).sum()),
            "risk_hours": int(risk.sum()),
            "mould_risk_pct": risk.sum() / n * 100 if n else np.nan,
            "n_runs": len(runs), "longest_run_h": max(runs) if runs else 0,
            "hum_mean": hum.mean(), "hum_p95": hum.quantile(0.95), "hum_max": hum.max(),
            "dew_point_mean": dp.mean(), "dew_point_max": dp.max(),
            "dew_margin_mean": margin.mean(), "dew_margin_min": margin.min(),
        })
    return pd.DataFrame(rows).round(2)


def mould_threshold_scan(df: pd.DataFrame,
                         thresholds: tuple[float, ...] = (50.0, 55.0, 60.0, 65.0, 70.0),
                         min_run: int = MOULD_RUN_H) -> pd.DataFrame:
    """Перевірка стійкості: ризик цвілі при різних порогах вологості."""
    rows = []
    for thr in thresholds:
        r = mould_risk(df, rh_threshold=thr, min_run=min_run)
        row = {"rh_threshold_pct": thr}
        for _, x in r.iterrows():
            row[ROOM_PREFIX[x["room"]]] = x["mould_risk_pct"]
        rows.append(row)
    return pd.DataFrame(rows).round(1)


def mould_by_month(df: pd.DataFrame, rh_threshold: float = MOULD_RH,
                   min_run: int = MOULD_RUN_H) -> pd.DataFrame:
    """Місячний розріз ризику цвілі (серії рахуємо на суцільному ряді, потім ріжемо)."""
    risk = {}
    for room in ROOMS:
        hum = df[hum_col(room)]
        valid = hum.notna()
        risk[room] = _wet_runs((hum >= rh_threshold).where(valid), min_run) & valid
    rows = []
    for month, chunk in df.groupby("month", sort=True):
        for room in ROOMS:
            r = risk[room].loc[chunk.index]
            n = int(chunk[hum_col(room)].notna().sum())
            if n == 0:
                continue
            rows.append({"month": month, "room": room, "room_ua": room_ua(room),
                         "n_hours": n, "risk_hours": int(r.sum()),
                         "mould_risk_pct": r.sum() / n * 100})
    return pd.DataFrame(rows).round(1)


# ── Графіки ───────────────────────────────────────────────────────────────────
def _save(fig: plt.Figure, root: Path, name: str) -> Path:
    path = Path(root) / "images" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


def set_style() -> None:
    sns.set_theme(style="whitegrid")
    plt.rcParams.update({"figure.dpi": 110, "font.size": 12,
                         "axes.titlesize": 14, "axes.labelsize": 12})


def plot_daily_cycle(cycle: pd.DataFrame, root: str | Path) -> Path:
    fig, ax = plt.subplots(figsize=(10, 4.5))
    for room in ROOMS:
        ax.plot(cycle.index, cycle[room], color=ROOM_COLORS[room],
                lw=2.5, marker="o", ms=4, label=room_ua(room))
    ax.plot(cycle.index, cycle["Outdoor"], color=OUT_COLOR, lw=2.5,
            ls="--", marker="o", ms=4, label=OUT_LABEL_UA)
    ax.set_xlabel("година доби (Київ)")
    ax.set_ylabel("середня температура, °C")
    ax.set_title("Добовий цикл: вулиця дихає, квартира — майже ні")
    ax.set_xticks(range(0, 24, 2))
    lo, hi = ax.get_ylim()
    ax.set_ylim(lo, hi + (hi - lo) * 0.18)
    ax.legend(loc="upper center", ncols=4, frameon=True, fontsize=11)
    return _save(fig, root, "m3_daily_cycle.png")


def plot_indoor_vs_outdoor(df: pd.DataFrame, root: str | Path) -> Path:
    weekly = df[[temp_col(r) for r in ROOMS] + ["out_temp"]].resample("7D").mean()
    fig, ax = plt.subplots(figsize=(10, 4.5))
    for room in ROOMS:
        ax.plot(weekly.index, weekly[temp_col(room)], color=ROOM_COLORS[room],
                lw=2.5, label=room_ua(room))
    ax.plot(weekly.index, weekly["out_temp"], color=OUT_COLOR, lw=2.5,
            ls="--", label=OUT_LABEL_UA)
    ax.set_xlabel("тиждень")
    ax.set_ylabel("температура, °C")
    ax.set_title("Пів року: надворі +25 °C розмаху, вдома — кілька")
    ax.legend(loc="upper left", ncols=2, frameon=True)
    fig.autofmt_xdate()
    return _save(fig, root, "m3_indoor_vs_outdoor.png")


def plot_scatter_sensitivity(df: pd.DataFrame, root: str | Path,
                             room: str = "Bedroom") -> Path:
    fit = sensitivity(df, room, daily=False)
    x, y = df["out_temp"], df[temp_col(room)]
    ok = x.notna() & y.notna()
    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.scatter(x[ok], y[ok], s=6, alpha=0.18, color=ROOM_COLORS[room],
               edgecolors="none", label="години")
    xs = np.linspace(x[ok].min(), x[ok].max(), 100)
    ax.plot(xs, fit["intercept"] + fit["slope"] * xs, color="#1B1B1B", lw=2.5,
            label=f"регресія: +1 °C надворі → +{fit['slope']:.2f} °C вдома")
    ax.set_xlabel("температура надворі, °C")
    ax.set_ylabel(f"температура: {room_ua(room)}, °C")
    ax.set_title(f"Чутливість: {room_ua(room)} проти вулиці (R² = {fit['r2']:.2f})")
    leg = ax.legend(loc="upper left", frameon=True, markerscale=4)
    leg.legend_handles[0].set_alpha(0.9)
    return _save(fig, root, "m3_scatter_sensitivity.png")


def plot_comfort(comf: pd.DataFrame, root: str | Path) -> Path:
    labels = [room_ua(r) for r in comf["room"]]
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5))
    specs = [
        (axes[0], ["too_cold_pct", "temp_ok_pct", "too_hot_pct"],
         ["холодно < 20 °C", "комфорт 20–24 °C", "спекотно > 24 °C"],
         ["#6BAED6", "#4C956C", "#C73E1D"], "Температура"),
        (axes[1], ["too_dry_pct", "hum_ok_pct", "too_humid_pct"],
         ["сухо < 30 %", "комфорт 30–60 %", "волого > 60 %"],
         ["#E0A458", "#4C956C", "#2E86AB"], "Вологість"),
    ]
    for ax, cols, names, colors, title in specs:
        bottom = np.zeros(len(comf))
        for col, name, color in zip(cols, names, colors):
            vals = comf[col].to_numpy(dtype=float)
            ax.bar(labels, vals, bottom=bottom, color=color, label=name, width=0.6)
            for xi, (v, b) in enumerate(zip(vals, bottom)):
                if v >= 7:
                    ax.text(xi, b + v / 2, f"{v:.0f}%", ha="center", va="center",
                            color="white", fontsize=11, fontweight="bold")
            bottom += vals
        ax.set_ylim(0, 100)
        ax.set_ylabel("% годин")
        ax.set_title(title)
        ax.legend(fontsize=9, loc="lower center", bbox_to_anchor=(0.5, -0.38), ncols=1)
    fig.suptitle("Комфорт: скільки часу квартира в межах норми", y=1.02)
    return _save(fig, root, "m3_comfort.png")


def plot_mould(df: pd.DataFrame, scan: pd.DataFrame, root: str | Path,
               rh_threshold: float = MOULD_RH) -> Path:
    """Вологість проти порога цвілі + перевірка стійкості по різних порогах."""
    daily = df[[hum_col(r) for r in ROOMS]].resample("1D").mean()
    fig, axes = plt.subplots(2, 1, figsize=(10, 7.5),
                             gridspec_kw={"height_ratios": [1.4, 1]})
    ax = axes[0]
    for room in ROOMS:
        ax.plot(daily.index, daily[hum_col(room)], color=ROOM_COLORS[room],
                lw=2, label=room_ua(room))
    ax.axhline(rh_threshold, color="#C73E1D", ls="--", lw=2.5)
    ax.text(daily.index[2], rh_threshold + 1.2, f"поріг ризику цвілі {rh_threshold:.0f} %",
            color="#C73E1D", fontsize=12, fontweight="bold")
    ax.set_ylim(22, 74)
    ax.set_ylabel("вологість, %\n(середнє за добу)")
    ax.set_title("Вологість жодного разу не дійшла до порога цвілі")
    ax.legend(loc="lower right", ncols=3, frameon=True, fontsize=11)

    ax = axes[1]
    xs = np.arange(len(scan))
    width = 0.8 / len(ROOMS)
    for i, room in enumerate(ROOMS):
        vals = scan[ROOM_PREFIX[room]].to_numpy(dtype=float)
        pos = xs + i * width - 0.4 + width / 2
        ax.bar(pos, vals, width=width, color=ROOM_COLORS[room], label=room_ua(room))
        for xi, v in zip(pos, vals):
            if v > 0:
                ax.text(xi, v + 0.15, f"{v:g}", ha="center", fontsize=10)
    ax.set_xticks(xs)
    ax.set_xticklabels([f"{t:.0f} %" for t in scan["rh_threshold_pct"]])
    ax.set_xlabel("поріг вологості")
    ax.set_ylabel("% годин у ризику")
    ax.set_title(f"Якщо знизити поріг: ризик при різних межах (серія ≥ {MOULD_RUN_H} год)")
    ax.legend(loc="upper right", ncols=3, frameon=True, fontsize=10)
    fig.tight_layout()
    return _save(fig, root, "m3_mould.png")


# ── Збірка результатів ────────────────────────────────────────────────────────
def build_results(df: pd.DataFrame, info: dict) -> tuple[dict, dict[str, pd.DataFrame]]:
    """Рахує всі метрики M3 і повертає (плоский json-словник, таблиці)."""
    desc = describe_rooms(df)
    monthly = monthly_table(df)
    cycle = daily_cycle(df)
    smooth = smoothing(df)
    lag = lags(df)
    sens = sensitivity_table(df)
    comf = comfort(df)
    comf_m = comfort(df, by_month=True)
    mould = mould_risk(df)
    mould_m = mould_by_month(df)
    mould_scan = mould_threshold_scan(df)

    res: dict = {
        "rows_hours": info["rows"],
        "stuck_hours_removed": info["stuck_hours_removed"],
        "ts_start_kyiv": info["ts_start_kyiv"],
        "ts_end_kyiv": info["ts_end_kyiv"],
        "rh_threshold_pct": MOULD_RH,
        "mould_min_run_h": MOULD_RUN_H,
        "comfort_temp_low_c": COMFORT_TEMP[0], "comfort_temp_high_c": COMFORT_TEMP[1],
        "comfort_hum_low_pct": COMFORT_HUM[0], "comfort_hum_high_pct": COMFORT_HUM[1],
        "out_mean_temp": round(float(df["out_temp"].mean()), 2),
        "out_min_temp": round(float(df["out_temp"].min()), 2),
        "out_max_temp": round(float(df["out_temp"].max()), 2),
        "out_mean_hum": round(float(df["out_hum"].mean()), 2),
        "outdoor_daily_range": round(float(daily_ranges(df, "out_temp").mean()), 2),
        "peak_hour_outdoor": int(cycle["Outdoor"].idxmax()),
    }
    for room in ROOMS:
        p = ROOM_PREFIX[room]
        d_t = desc[(desc.room == room) & (desc.metric == "температура")].iloc[0]
        d_h = desc[(desc.room == room) & (desc.metric == "вологість")].iloc[0]
        sm_r = smooth[smooth.room == room].iloc[0]
        lg = lag[lag.room == room].iloc[0]
        sh = sens[(sens.room == room) & (sens.scope == "hourly")].iloc[0]
        sd = sens[(sens.room == room) & (sens.scope == "daily")].iloc[0]
        cf = comf[comf.room == room].iloc[0]
        mr = mould[mould.room == room].iloc[0]
        res |= {
            f"{p}_mean_temp": float(d_t["mean"]), f"{p}_min_temp": float(d_t["min"]),
            f"{p}_max_temp": float(d_t["max"]), f"{p}_std_temp": float(d_t["std"]),
            f"{p}_mean_hum": float(d_h["mean"]), f"{p}_std_hum": float(d_h["std"]),
            f"{p}_n_hours": int(d_t["n_hours"]),
            f"{p}_peak_hour": int(cycle[room].idxmax()),
            f"{p}_cycle_amplitude": round(float(cycle[room].max() - cycle[room].min()), 2),
            f"{p}_indoor_daily_range": float(sm_r["indoor_daily_range"]),
            f"{p}_smoothing_x": float(sm_r["smoothing_x"]),
            f"{p}_lag_h": int(lg["lag_h"]), f"{p}_lag_corr": float(lg["corr_at_lag"]),
            f"{p}_lag_h_diff": int(lg["lag_h_diff"]),
            f"{p}_lag_identifiable": bool(lg["lag_identifiable"]),
            f"{p}_sensitivity": round(float(sh["slope"]), 3),
            f"{p}_sensitivity_ci_low": round(float(sh["ci_low"]), 3),
            f"{p}_sensitivity_ci_high": round(float(sh["ci_high"]), 3),
            f"{p}_r2": round(float(sh["r2"]), 3),
            f"{p}_sensitivity_daily": round(float(sd["slope"]), 3),
            f"{p}_sensitivity_daily_ci_low": round(float(sd["ci_low"]), 3),
            f"{p}_sensitivity_daily_ci_high": round(float(sd["ci_high"]), 3),
            f"{p}_r2_daily": round(float(sd["r2"]), 3),
            f"{p}_comfort_temp_ok_pct": float(cf["temp_ok_pct"]),
            f"{p}_too_cold_pct": float(cf["too_cold_pct"]),
            f"{p}_too_hot_pct": float(cf["too_hot_pct"]),
            f"{p}_comfort_hum_ok_pct": float(cf["hum_ok_pct"]),
            f"{p}_too_dry_pct": float(cf["too_dry_pct"]),
            f"{p}_too_humid_pct": float(cf["too_humid_pct"]),
            f"{p}_mould_risk_pct": float(mr["mould_risk_pct"]),
            f"{p}_mould_risk_hours": int(mr["risk_hours"]),
            f"{p}_mould_longest_run_h": int(mr["longest_run_h"]),
            f"{p}_hum_max": float(mr["hum_max"]),
            f"{p}_hum_p95": float(mr["hum_p95"]),
            f"{p}_dew_point_mean": float(mr["dew_point_mean"]),
            f"{p}_dew_margin_mean": float(mr["dew_margin_mean"]),
            f"{p}_dew_margin_min": float(mr["dew_margin_min"]),
        }
    res["mould_risk_pct_living_room"] = res["bedroom_mould_risk_pct"]
    res["mould_risk_pct_max_room"] = max(
        float(res[f"{ROOM_PREFIX[r]}_mould_risk_pct"]) for r in ROOMS)
    res["mould_risk_pct_at_60"] = float(
        mould_scan.loc[mould_scan.rh_threshold_pct == 60.0, "bedroom"].iloc[0])
    res["mould_risk_pct_at_55"] = float(
        mould_scan.loc[mould_scan.rh_threshold_pct == 55.0, "bedroom"].iloc[0])
    res["smoothing_x_best_room"] = max(ROOMS, key=lambda r: res[f"{ROOM_PREFIX[r]}_smoothing_x"])
    res["sensitivity_mean"] = round(
        float(np.mean([res[f"{ROOM_PREFIX[r]}_sensitivity"] for r in ROOMS])), 3)
    res["comfort_temp_ok_pct_living_room"] = res["bedroom_comfort_temp_ok_pct"]
    res["comfort_hum_ok_pct_living_room"] = res["bedroom_comfort_hum_ok_pct"]

    tables = {
        "describe": desc, "monthly": monthly, "daily_cycle": cycle,
        "smoothing": smooth, "lags": lag, "sensitivity": sens,
        "comfort": comf, "comfort_monthly": comf_m,
        "mould": mould, "mould_monthly": mould_m, "mould_scan": mould_scan,
    }
    return res, tables


def save_results(res: dict, tables: dict[str, pd.DataFrame],
                 root: str | Path = ".") -> list[Path]:
    """Пише results/m3_*.json і results/m3_*.csv за контрактом проєкту."""
    root = Path(root)
    rdir = root / "results"
    rdir.mkdir(parents=True, exist_ok=True)
    paths = [rdir / "m3_climate.json"]
    (rdir / "m3_climate.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")

    tables["daily_cycle"].to_csv(rdir / "m3_daily_cycle.csv", encoding="utf-8")
    paths.append(rdir / "m3_daily_cycle.csv")

    monthly = tables["monthly"].merge(
        tables["mould_monthly"][["month", "room", "mould_risk_pct"]],
        on=["month", "room"], how="left")
    monthly.to_csv(rdir / "m3_monthly.csv", index=False, encoding="utf-8")
    paths.append(rdir / "m3_monthly.csv")

    comf = pd.concat([
        tables["comfort"].assign(month="усе"),
        tables["comfort_monthly"],
    ], ignore_index=True)
    comf.to_csv(rdir / "m3_comfort.csv", index=False, encoding="utf-8")
    paths.append(rdir / "m3_comfort.csv")
    return paths


def run_all(root: str | Path = ".") -> dict:
    """Повний прогін M3: числа, таблиці, графіки."""
    df, info = load_climate(root)
    res, tables = build_results(df, info)
    save_results(res, tables, root)
    set_style()
    plot_daily_cycle(tables["daily_cycle"], root)
    plot_indoor_vs_outdoor(df, root)
    plot_scatter_sensitivity(df, root)
    plot_comfort(tables["comfort"], root)
    plot_mould(df, tables["mould_scan"], root)
    return res


def main() -> None:  # pragma: no cover
    res = run_all(Path(__file__).resolve().parents[1])
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":  # pragma: no cover
    main()
