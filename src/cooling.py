"""Модуль M4 — скільки тепла тримає квартира (проєкт «4331 година»).

Питання: **скільки градусів за годину втрачає квартира, коли зникає світло?**

Фізична модель — закон охолодження Ньютона:

```
T(t) = T_вул + (T0 − T_вул) · e^(−k·t)
```

`k` (1/год) — коефіцієнт втрати тепла, головне число проєкту.
`τ = 1/k` — теплова стала часу квартири: за τ годин різниця з вулицею
падає в e ≈ 2.72 раза.

## Пастка, яку не можна замовчати

Усі кімнатні датчики живляться від розетки. Під час відключення вони
**мовчать**. Тому *всередині* події немає жодного показу — є лише
останній показ до неї (`T0`) і перший після (`T_end`). Підгонка кривої
по події — це підгонка по **двох точках**: одне `k` на подію, а не крива.

Тому модуль рахує `k` двома способами і чесно їх порівнює:

| Метод | Дані | Сильна сторона | Слабка сторона |
|---|---|---|---|
| **A. Дві точки** | 5 відключень × 3 кімнати | це справжній блекаут | влітку `T0 − T_вул` мале → `k` нестійке |
| **B. Нічний спад** | 6 місяців, ночі 23:00–06:00 | тисячі точок, вузький CI | це природна стала квартири, а не блекаут |

Метод B міряє `ΔT_in(t+1) − T_in(t) = a − k·(T_in(t) − T_out(t))` звичайним
МНК (`statsmodels`). Ночі — бо вдень сонце й техніка гріють квартиру саме
тоді, коли надворі тепло, і наївна підгонка по всіх годинах дає **від'ємне**
`k` (є в метриках як перевірка на чутливість).

⚠️ Що саме міряє `k` методу B: природну теплову сталу квартири **разом із
теплом сусідів крізь стіни й зі стояками опалення**. Це не «порожня коробка
на морозі». Справжній зимовий блекаут з холодними батареями перерахуємо,
коли з'являться зимові дані.

Запуск: `PYTHONUTF8=1 python -m src.cooling`

Результати:
  * `results/m4_cooling.json`     — метрики (контракт для M9-Excel, дашборда, README);
  * `results/m4_events.csv`       — таблиця «подія × кімната» методу A;
  * `results/m4_hours_table.csv`  — годин до 18 °C за температурою надворі;
  * `images/m4_*.png`             — чотири графіки.
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
import statsmodels.api as sm

from .cleaning import KYIV, ROOM_COLORS, ROOM_LABELS_UA

# ── Довідники й пороги ────────────────────────────────────────────────────────

#: Кімната → колонка в `hours_wide.csv`.
ROOM_COLS: dict[str, str] = {
    "Bedroom": "bedroom_temp",
    "Kitchen": "kitchen_temp",
    "Printernya": "printernya_temp",
}
ROOMS: list[str] = list(ROOM_COLS)

#: Ключ у JSON для кожної кімнати (нижній регістр англійської назви).
ROOM_KEYS: dict[str, str] = {r: r.lower() for r in ROOMS}

#: Метод A: найближчий показ має бути не далі ніж за стільки годин від події.
MAX_LAG_H = 3.0

#: Метод A: якщо |T0 − T_вул| менше — задача погано обумовлена, рядок не беремо.
MIN_DELTA_C = 3.0

#: Метод B: години київського часу, з яких стартує погодинний крок (23:00–06:00).
NIGHT_START_HOURS: list[int] = [23, 0, 1, 2, 3, 4, 5]

#: Поріг комфорту й типовий старт для калькулятора.
THRESHOLD_C = 18.0
T0_DEFAULT_C = 22.0

#: Сітка температур надворі для таблиці «годин до 18 °C».
OUT_GRID: list[float] = [-20, -15, -10, -5, 0, 5, 10]

#: Колір «модель» на графіках (виміряне — кольором кімнати).
MODEL_COLOR = "#6C757D"
OUT_COLOR = "#6C757D"

EVENT_COLS = ["event_id", "room", "room_ua", "start_kyiv", "hours",
              "t0_c", "t_end_c", "t_out_c", "delta_t0_c", "delta_end_c",
              "drop_c", "k_per_h", "tau_h", "used", "reason"]


# ── A. Фізика ─────────────────────────────────────────────────────────────────
def newton(t, T0: float, T_out: float, k: float):
    """Закон охолодження Ньютона: температура в кімнаті через `t` годин.

    `T(t) = T_вул + (T0 − T_вул)·e^(−k·t)`. Приймає число або масив `t`.
    """
    return T_out + (T0 - T_out) * np.exp(-k * np.asarray(t, dtype=float))


def hours_to_threshold(k: float, T0: float = T0_DEFAULT_C,
                       T_out: float = -10.0, target: float = THRESHOLD_C) -> float:
    """Скільки годин від `T0` до порога `target` при температурі надворі `T_out`.

    `годин = ln((T0 − T_вул) / (поріг − T_вул)) / k` — та сама формула, що піде
    в Excel-калькулятор (M9). `NaN`, якщо поріг недосяжний (надворі тепліше
    за поріг) або `k` не додатне.
    """
    if not np.isfinite(k) or k <= 0:
        return float("nan")
    num, den = T0 - T_out, target - T_out
    if num <= 0 or den <= 0 or num <= den:
        return float("nan")
    return float(np.log(num / den) / k)


def two_point_k(T0: float, T_end: float, T_out: float, hours: float) -> float:
    """`k` за двома точками: `k = −ln((T_end − T_вул)/(T0 − T_вул)) / години`.

    `NaN`, якщо відношення не додатне (різниця з вулицею змінила знак) або
    кімната за подію нагрілась.
    """
    d0, d1 = T0 - T_out, T_end - T_out
    if not np.isfinite(d0) or not np.isfinite(d1) or d0 == 0 or hours <= 0:
        return float("nan")
    ratio = d1 / d0
    if ratio <= 0:
        return float("nan")
    return float(-np.log(ratio) / hours)


# ── B. Дані ───────────────────────────────────────────────────────────────────
def load_clean(root: str | Path = ".") -> tuple[pd.DataFrame, pd.DataFrame]:
    """Читає `hours_wide.csv` + таблицю подій M2; глушить `stuck`-години.

    Застряглий датчик (прапорець `stuck` з M1) — не свідок: такі години
    зануляємо в `NaN`, щоб вони не потрапили ні в метод A, ні в метод B.
    Повертає (погодинна широка таблиця з індексом UTC, події M2).
    """
    root = Path(root)
    wide = pd.read_csv(root / "data" / "clean" / "hours_wide.csv")
    wide["ts"] = pd.to_datetime(wide["ts"], utc=True, format="mixed")

    hourly = pd.read_csv(root / "data" / "clean" / "readings_hourly.csv",
                         usecols=["ts", "room", "metric", "stuck"])
    hourly["ts"] = pd.to_datetime(hourly["ts"], utc=True, format="mixed")
    hourly["stuck"] = hourly["stuck"].astype(bool)
    for room, col in ROOM_COLS.items():
        bad = hourly.loc[hourly["stuck"] & (hourly["room"] == room)
                         & (hourly["metric"] == "temperature"), "ts"]
        wide.loc[wide["ts"].isin(set(bad)), col] = np.nan

    wide = wide.set_index("ts").sort_index()
    full = pd.date_range(wide.index.min(), wide.index.max(), freq="h", tz="UTC")
    wide = wide.reindex(full)
    wide.index.name = "ts"
    wide["hour_kyiv"] = wide.index.tz_convert(KYIV).hour

    events = pd.read_csv(root / "results" / "m2_outages.csv")
    for c in ("start_utc", "end_utc"):
        events[c] = pd.to_datetime(events[c], utc=True, format="mixed")
    return wide, events


# ── C. Метод A — дві точки на подію ───────────────────────────────────────────
def event_two_point_table(wide: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    """Таблиця «подія × кімната»: T0, T_end, T_вул, години, `k` — і чому рядок відкинули.

    `T0` — останній показ *перед* стартом події, `T_end` — перший показ *після*
    її кінця, `T_вул` — середня температура надворі за години події.
    Рядок беремо в підсумок лише якщо: показ поруч із подією (≤ `MAX_LAG_H`),
    |T0 − T_вул| ≥ `MIN_DELTA_C`, різниця не змінила знак і `k > 0`.
    """
    rows = []
    for room, col in ROOM_COLS.items():
        s = wide[col].dropna()
        for e in events.itertuples():
            before = s[s.index < e.start_utc]
            after = s[s.index >= e.end_utc]
            t0 = float(before.iloc[-1]) if len(before) else np.nan
            t_end = float(after.iloc[0]) if len(after) else np.nan
            lag0 = ((e.start_utc - before.index[-1]) / pd.Timedelta("1h")
                    if len(before) else np.nan)
            lag1 = ((after.index[0] - e.end_utc) / pd.Timedelta("1h")
                    if len(after) else np.nan)
            win = wide.loc[(wide.index >= e.start_utc) & (wide.index < e.end_utc), "out_temp"]
            t_out = float(win.mean())
            d0, d1 = t0 - t_out, t_end - t_out

            reason = ""
            if not np.isfinite(t0) or not np.isfinite(t_end):
                reason = "датчик ще не працював — немає показу біля події"
            elif max(lag0, lag1) > MAX_LAG_H:
                reason = f"найближчий показ аж за {max(lag0, lag1):.0f} год від події"
            elif abs(d0) < MIN_DELTA_C:
                reason = f"мала різниця з вулицею: T0 − T_вул = {d0:+.1f} °C"
            elif d0 * d1 <= 0:
                reason = "різниця з вулицею змінила знак"

            k = two_point_k(t0, t_end, t_out, e.hours) if not reason else np.nan
            if not reason and not np.isfinite(k):
                reason = "кімната за подію не охолола (k не визначене)"
            elif not reason and k <= 0:
                reason = f"k ≤ 0 — кімната нагрілась ({k:+.4f})"
                k = np.nan

            rows.append({
                "event_id": e.event_id, "room": room,
                "room_ua": ROOM_LABELS_UA.get(room, room),
                "start_kyiv": str(e.start_kyiv), "hours": float(e.hours),
                "t0_c": t0, "t_end_c": t_end, "t_out_c": t_out,
                "delta_t0_c": d0, "delta_end_c": d1,
                "drop_c": t_end - t0, "k_per_h": k,
                "tau_h": (1 / k if np.isfinite(k) and k > 0 else np.nan),
                "used": reason == "", "reason": reason,
            })
    tab = pd.DataFrame(rows, columns=EVENT_COLS)
    tab = tab.sort_values(["event_id", "room"], ignore_index=True)
    return tab


def two_point_summary(tab: pd.DataFrame) -> dict:
    """Медіана / діапазон `k` методу A по кімнатах + скільки подій вижило."""
    good = tab[tab["used"]]
    out: dict = {
        "n_rows_total": int(len(tab)),
        "n_rows_used": int(len(good)),
        "n_events_used": int(good["event_id"].nunique()),
        "n_events_total": int(tab["event_id"].nunique()),
    }
    for room in ROOMS:
        g = good[good["room"] == room]["k_per_h"]
        key = ROOM_KEYS[room]
        out[f"k_{key}_two_point_median"] = float(g.median()) if len(g) else float("nan")
        out[f"k_{key}_two_point_min"] = float(g.min()) if len(g) else float("nan")
        out[f"k_{key}_two_point_max"] = float(g.max()) if len(g) else float("nan")
        out[f"n_{key}_two_point"] = int(len(g))
    return out


def best_event_row(tab: pd.DataFrame) -> pd.Series | None:
    """Найкраще обумовлений рядок методу A — з найбільшою |T0 − T_вул|."""
    good = tab[tab["used"]].copy()
    if not len(good):
        return None
    good["cond"] = good["delta_t0_c"].abs()
    return good.sort_values("cond", ascending=False).iloc[0]


# ── D. Метод B — нічний спад, МНК на 6 місяцях ────────────────────────────────
def night_decay_frame(wide: pd.DataFrame, room: str, night_only: bool = True) -> pd.DataFrame:
    """Точки для регресії: `x = T_in − T_out`, `y = ΔT_in` за наступну годину.

    Беремо крок `t → t+1` лише якщо **обидві** години мають дані і (для нічної
    вибірки) **весь** крок лежить у вікні 23:00–06:00 за київським часом.
    """
    col = ROOM_COLS[room]
    hk = pd.Series(wide["hour_kyiv"].to_numpy(), index=wide.index)
    d = pd.DataFrame({
        "T": wide[col],
        "O": wide["out_temp"],
        "T_next": wide[col].shift(-1),
        "hk": hk,
        "hk_next": hk.shift(-1),
    })
    d["y"] = d["T_next"] - d["T"]
    d["x"] = d["T"] - d["O"]
    mask = d[["x", "y"]].notna().all(axis=1)
    if night_only:
        end_hours = [(h + 1) % 24 for h in NIGHT_START_HOURS]
        mask &= d["hk"].isin(NIGHT_START_HOURS) & d["hk_next"].isin(end_hours)
    return d.loc[mask, ["x", "y"]]


def fit_night_decay(wide: pd.DataFrame, room: str, night_only: bool = True) -> dict:
    """МНК `y = a − k·x` для однієї кімнати. Повертає `k`, CI, `R²`, `n`, `τ`.

    `k = −нахил`. Довірчий інтервал — 95 %, звичайний OLS (залишки слабко
    автокорельовані, тож CI радше оптимістичний — сказано в «Обмеженнях»).
    """
    d = night_decay_frame(wide, room, night_only=night_only)
    if len(d) < 30:
        return {"room": room, "n_points": int(len(d)), "k": float("nan"),
                "ci_low": float("nan"), "ci_high": float("nan"),
                "a": float("nan"), "r2": float("nan"), "tau_h": float("nan"),
                "p_value": float("nan"), "night_only": night_only}
    res = sm.OLS(d["y"], sm.add_constant(d["x"])).fit()
    slope_ci = res.conf_int().loc["x"]
    k = float(-res.params["x"])
    return {
        "room": room,
        "n_points": int(res.nobs),
        "k": k,
        "ci_low": float(-slope_ci.iloc[1]),
        "ci_high": float(-slope_ci.iloc[0]),
        "a": float(res.params["const"]),
        "r2": float(res.rsquared),
        "p_value": float(res.pvalues["x"]),
        "tau_h": float(1 / k) if k > 0 else float("nan"),
        "night_only": night_only,
        "_res": res,
    }


def fit_all_rooms(wide: pd.DataFrame, night_only: bool = True) -> dict[str, dict]:
    """Метод B для всіх трьох кімнат."""
    return {room: fit_night_decay(wide, room, night_only=night_only) for room in ROOMS}


# ── E. Практичний результат — годин до 18 °C ──────────────────────────────────
def hours_table(k_by_room: dict[str, float], T0: float = T0_DEFAULT_C,
                target: float = THRESHOLD_C,
                out_grid: list[float] | None = None) -> pd.DataFrame:
    """Таблиця «скільки годин від 22 °C до 18 °C» за температурою надворі.

    Рядки — `T_вул` із сітки, колонки — години по кімнатах (+ швидкість
    падіння в першу годину, `k·(T0 − T_вул)`).
    """
    grid = OUT_GRID if out_grid is None else out_grid
    rows = []
    for t_out in grid:
        row = {"t_out_c": float(t_out), "t0_c": float(T0), "target_c": float(target)}
        for room in ROOMS:
            key = ROOM_KEYS[room]
            k = k_by_room.get(room, float("nan"))
            row[f"hours_{key}"] = hours_to_threshold(k, T0=T0, T_out=t_out, target=target)
            row[f"rate_c_per_h_{key}"] = (float(k * (T0 - t_out))
                                          if np.isfinite(k) else float("nan"))
        rows.append(row)
    return pd.DataFrame(rows)


# ── F. Метрики (контракт результатів) ─────────────────────────────────────────
def metrics(tab: pd.DataFrame, fits: dict[str, dict], fits_all: dict[str, dict],
            htab: pd.DataFrame) -> dict:
    """Плаский словник для `results/m4_cooling.json` — його читають M9, дашборд, README."""
    m: dict = {"k_method": "night_decay_ols",
               "k_unit": "1/год",
               "night_window_kyiv": "23:00–06:00",
               "threshold_c": THRESHOLD_C,
               "t0_default_c": T0_DEFAULT_C}

    for room in ROOMS:
        key, f, fa = ROOM_KEYS[room], fits[room], fits_all[room]
        m[f"k_{key}"] = f["k"]
        m[f"k_{key}_ci_low"] = f["ci_low"]
        m[f"k_{key}_ci_high"] = f["ci_high"]
        m[f"tau_{key}_h"] = f["tau_h"]
        m[f"r2_{key}"] = f["r2"]
        m[f"n_points_{key}"] = f["n_points"]
        m[f"p_value_{key}"] = f["p_value"]
        m[f"k_{key}_ci_crosses_zero"] = bool(f["ci_low"] <= 0 <= f["ci_high"])
        m[f"k_{key}_all_hours"] = fa["k"]
        m[f"n_points_{key}_all_hours"] = fa["n_points"]

    # Контрактні псевдоніми «без кімнати» — це житлова кімната, головна в проєкті.
    m["r2"] = fits["Bedroom"]["r2"]
    m["n_points"] = fits["Bedroom"]["n_points"]

    m.update(two_point_summary(tab))

    k_bed = fits["Bedroom"]["k"]
    m["cooling_rate_c_per_h_at_minus10"] = float(k_bed * (T0_DEFAULT_C - (-10.0)))
    for t_out, suffix in [(-20, "minus20"), (-15, "minus15"), (-10, "minus10"),
                          (-5, "minus5"), (0, "0"), (5, "5"), (10, "10")]:
        m[f"hours_to_18_from_22_at_{suffix}"] = hours_to_threshold(
            k_bed, T0=T0_DEFAULT_C, T_out=float(t_out), target=THRESHOLD_C)

    best = best_event_row(tab)
    if best is not None:
        m["best_event_id"] = str(best["event_id"])
        m["best_event_room"] = str(best["room"])
        m["best_event_delta_t0_c"] = float(best["delta_t0_c"])
        m["best_event_k"] = float(best["k_per_h"])

    m["hours_table_max_h"] = float(np.nanmax(htab["hours_bedroom"].to_numpy()))
    m["limitation_note"] = (
        "k міряє природну теплову сталу квартири разом із теплом сусідів крізь "
        "стіни: всі п'ять відключень — літні, надворі було майже так само тепло, "
        "як у квартирі. Зимовий блекаут з холодними батареями перерахуємо, "
        "коли з'являться зимові дані."
    )
    m["two_points_note"] = (
        "Розеткові датчики мовчать під час відключення, тож усередині події "
        "показів немає: лише T0 (останній до) і T_end (перший після). "
        "Метод A дає одне k на подію, а не криву."
    )
    return m


# ── G. Графіки ────────────────────────────────────────────────────────────────
def _style() -> None:
    sns.set_theme(style="whitegrid")
    plt.rcParams.update({"font.size": 12, "axes.titlesize": 14, "axes.labelsize": 12})


def _save(fig, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


def plot_event_two_point(tab: pd.DataFrame, wide: pd.DataFrame, events: pd.DataFrame,
                         out: Path) -> Path:
    """Кожне відключення: дві виміряні точки на кімнату + смуга температури надворі."""
    _style()
    fig, ax = plt.subplots(figsize=(11, 5))
    ids = list(events["event_id"])
    offs = {"Bedroom": -0.22, "Kitchen": 0.0, "Printernya": 0.22}

    lo, hi = [], []
    for i, e in enumerate(events.itertuples()):
        win = wide.loc[(wide.index >= e.start_utc) & (wide.index < e.end_utc), "out_temp"]
        ax.add_patch(plt.Rectangle((i - 0.38, float(win.min())), 0.76,
                                   max(float(win.max() - win.min()), 0.15),
                                   color=OUT_COLOR, alpha=.25, zorder=0))
        ax.hlines(float(win.mean()), i - 0.38, i + 0.38, color=OUT_COLOR, lw=2, zorder=1)
        lo.append(float(win.min()))
        hi.append(float(win.max()))

    for r in tab.itertuples():
        i = ids.index(r.event_id)
        x = i + offs[r.room]
        color = ROOM_COLORS[r.room]
        if not np.isfinite(r.t0_c) or not np.isfinite(r.t_end_c):
            continue
        alpha = 1.0 if r.used else .25
        ax.plot([x, x], [r.t0_c, r.t_end_c], color=color, lw=2, alpha=alpha, zorder=2)
        ax.scatter([x], [r.t0_c], color=color, s=70, zorder=3, alpha=alpha,
                   edgecolor="white", linewidth=1.2)
        ax.scatter([x], [r.t_end_c], color=color, s=70, zorder=3, alpha=alpha,
                   marker="v", edgecolor="white", linewidth=1.2)
        if r.used:
            ax.annotate(f"{r.drop_c:+.1f}", (x, min(r.t0_c, r.t_end_c) - .35),
                        ha="center", va="top", fontsize=9, color=color)

    ax.set_xticks(range(len(ids)))
    ax.set_xticklabels([f"{e.event_id}\n{str(e.start_kyiv)[:10]}\n{e.hours:.0f} год"
                        for e in events.itertuples()], fontsize=10)
    ax.set_ylabel("температура, °C")
    ax.set_title("Що ми взагалі бачимо: дві точки на подію\n"
                 "● до відключення · ▼ після · сіра смуга — надворі")
    handles = [plt.Line2D([], [], color=ROOM_COLORS[r], lw=3,
                          label=ROOM_LABELS_UA.get(r, r)) for r in ROOMS]
    handles.append(plt.Line2D([], [], color=OUT_COLOR, lw=3, alpha=.5, label="надворі"))
    ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(.5, -0.16),
              fontsize=10, ncol=4, frameon=False)
    ax.set_xlim(-0.6, len(ids) - 0.4)
    y_lo = min(lo + tab["t_end_c"].dropna().tolist())
    y_hi = max(hi + tab["t0_c"].dropna().tolist() + tab["t_end_c"].dropna().tolist())
    ax.set_ylim(y_lo - 2.5, y_hi + 2.0)
    return _save(fig, out)


def plot_night_decay(wide: pd.DataFrame, fit: dict, out: Path,
                     room: str = "Bedroom") -> Path:
    """Метод B: хмара нічних точок `ΔT` проти `T_in − T_out` і підігнана пряма."""
    _style()
    d = night_decay_frame(wide, room, night_only=True)
    color = ROOM_COLORS[room]
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.axhline(0, color="#ADB5BD", lw=1)
    ax.scatter(d["x"], d["y"], s=14, alpha=.28, color=color,
               edgecolor="none", label=f"нічні години (n = {len(d)})")
    xs = np.linspace(float(d["x"].min()), float(d["x"].max()), 100)
    ax.plot(xs, fit["a"] - fit["k"] * xs, color="#212529", lw=2.5,
            label=f"МНК: ΔT = {fit['a']:+.3f} − {fit['k']:.4f}·x")
    ax.fill_between(xs, fit["a"] - fit["ci_high"] * xs, fit["a"] - fit["ci_low"] * xs,
                    color="#212529", alpha=.15, label="95 % довірчий інтервал")
    ax.set_xlabel("наскільки в кімнаті тепліше, ніж надворі, °C")
    ax.set_ylabel("зміна за годину, °C")
    ax.set_title(f"Метод B — нічний спад, {ROOM_LABELS_UA.get(room, room)}\n"
                 f"k = {fit['k']:.4f} 1/год  ·  τ = {fit['tau_h']:.0f} год  ·  "
                 f"R² = {fit['r2']:.3f}")
    ax.set_ylim(-1.2, 1.2)
    ax.legend(loc="upper right", fontsize=10)
    return _save(fig, out)


def plot_newton_curve(best: pd.Series | None, k_model: float, out: Path) -> Path:
    """Демонстрація кривої Ньютона: зліва реальна подія, справа зимовий сценарій."""
    _style()
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.8))

    ax = axes[0]
    if best is not None:
        room = str(best["room"])
        color = ROOM_COLORS.get(room, "#2E86AB")
        T0, T_end = float(best["t0_c"]), float(best["t_end_c"])
        T_out, hrs = float(best["t_out_c"]), float(best["hours"])
        k_ev = float(best["k_per_h"])
        t = np.linspace(0, 120, 300)          # 5 діб — щоб було видно вигин кривої
        ax.axhline(T_out, color=OUT_COLOR, ls="--", lw=1.8,
                   label=f"надворі {T_out:.1f} °C")
        ax.plot(t, newton(t, T0, T_out, k_ev), color=color, lw=2.5,
                label=f"модель по цій події, k = {k_ev:.4f}")
        ax.plot(t, newton(t, T0, T_out, k_model), color=MODEL_COLOR, lw=2.5, ls=":",
                label=f"модель методу B, k = {k_model:.4f}")
        ax.scatter([0, hrs], [T0, T_end], s=120, color=color, zorder=5,
                   edgecolor="white", linewidth=1.5, label="виміряно (2 точки)")
        ax.annotate("T0", (0, T0), textcoords="offset points", xytext=(6, 8), fontsize=11)
        ax.annotate("T_end", (hrs, T_end), textcoords="offset points",
                    xytext=(6, -14), fontsize=11)
        ax.axvspan(0, hrs, color="#C73E1D", alpha=.09)
        ax.annotate(f"виміряно лише тут — {hrs:.0f} год,\nдалі це вже модель",
                    (hrs + 4, T_out + 1.0), ha="left", fontsize=9.5, color="#C73E1D")
        ax.set_ylim(T_out - 1.5, T0 + 2.0)
        ax.set_title(f"{best['event_id']} · {ROOM_LABELS_UA.get(room, room)}\n"
                     f"найбільша різниця з вулицею: {best['delta_t0_c']:.1f} °C")
    ax.set_xlabel("годин без світла")
    ax.set_ylabel("температура, °C")
    ax.legend(fontsize=9, loc="best")

    ax = axes[1]
    T0, T_out = T0_DEFAULT_C, -10.0
    hrs18 = hours_to_threshold(k_model, T0=T0, T_out=T_out)
    t = np.linspace(0, max(hrs18 * 1.4, 12), 300)
    ax.plot(t, newton(t, T0, T_out, k_model), color=ROOM_COLORS["Bedroom"], lw=2.5,
            label=f"модель, k = {k_model:.4f}")
    ax.axhline(THRESHOLD_C, color="#C73E1D", ls="--", lw=1.8, label="поріг 18 °C")
    if np.isfinite(hrs18):
        ax.vlines(hrs18, THRESHOLD_C, T0, color="#C73E1D", lw=1.5, ls=":")
        ax.annotate(f"{hrs18:.0f} год", (hrs18, (T0 + THRESHOLD_C) / 2),
                    textcoords="offset points", xytext=(8, 0), fontsize=12, color="#C73E1D")
    ax.scatter([0], [T0], s=120, color=ROOM_COLORS["Bedroom"], zorder=5,
               edgecolor="white", linewidth=1.5)
    ax.set_xlabel("годин без світла")
    ax.set_ylabel("температура, °C")
    ax.set_title(f"Зимовий сценарій: старт {T0:.0f} °C, надворі {T_out:.0f} °C\n"
                 "(це модель, не вимір)")
    ax.legend(fontsize=9, loc="best")

    fig.suptitle("Крива Ньютона: суцільна лінія — модель, точки — виміряне", fontsize=13)
    fig.tight_layout()
    return _save(fig, out)


def plot_hours_table(htab: pd.DataFrame, out: Path) -> Path:
    """Головний практичний результат: годин від 22 °C до 18 °C по кімнатах."""
    _style()
    fig, ax = plt.subplots(figsize=(11, 5))
    x = np.arange(len(htab))
    width = 0.26
    cols = [f"hours_{ROOM_KEYS[r]}" for r in ROOMS]
    vmax = float(np.nanmax(htab[cols].to_numpy(dtype=float)))
    for i, room in enumerate(ROOMS):
        key = ROOM_KEYS[room]
        vals = htab[f"hours_{key}"].to_numpy(dtype=float)
        pos = x + (i - 1) * width
        ax.bar(pos, vals, width, color=ROOM_COLORS[room],
               label=ROOM_LABELS_UA.get(room, room))
        for p, v in zip(pos, vals):
            if np.isfinite(v):
                ax.text(p, v + vmax * .012, f"{v:.0f}", ha="center", fontsize=9)
    ax.set_ylim(0, vmax * 1.12)
    ax.set_xticks(x)
    ax.set_xticklabels([f"{v:+.0f} °C" for v in htab["t_out_c"]])
    ax.set_xlabel("температура надворі")
    ax.set_ylabel("годин")
    ax.set_title("Скільки годин від 22 °C до 18 °C\n(модель методу B — природна стала квартири)")
    ax.legend(fontsize=10)
    return _save(fig, out)


# ── H. Прогін ─────────────────────────────────────────────────────────────────
def run_all(root: str | Path = ".") -> dict:
    """Повний прогін M4: обидва методи, три таблиці результатів, чотири графіки."""
    root = Path(root)
    wide, events = load_clean(root)

    tab = event_two_point_table(wide, events)
    fits = fit_all_rooms(wide, night_only=True)
    fits_all = fit_all_rooms(wide, night_only=False)
    k_by_room = {room: fits[room]["k"] for room in ROOMS}
    htab = hours_table(k_by_room)
    m = metrics(tab, fits, fits_all, htab)

    res = root / "results"
    res.mkdir(parents=True, exist_ok=True)
    tab.to_csv(res / "m4_events.csv", index=False)
    htab.round(3).to_csv(res / "m4_hours_table.csv", index=False)
    (res / "m4_cooling.json").write_text(
        json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")

    img = root / "images"
    plot_event_two_point(tab, wide, events, img / "m4_event_two_point.png")
    plot_night_decay(wide, fits["Bedroom"], img / "m4_night_decay.png")
    plot_newton_curve(best_event_row(tab), fits["Bedroom"]["k"], img / "m4_newton_curve.png")
    plot_hours_table(htab, img / "m4_hours_table.png")
    return m


def main() -> None:
    matplotlib.use("Agg")
    m = run_all(Path(__file__).resolve().parents[1])
    print(f"k (метод B, ночі): кімната {m['k_bedroom']:.5f} | кухня {m['k_kitchen']:.5f} | "
          f"кладова {m['k_printernya']:.5f} 1/год")
    print(f"τ кімнати: {m['tau_bedroom_h']:.0f} год | "
          f"k методу A (медіана, кімната): {m['k_bedroom_two_point_median']:.5f} "
          f"по {m['n_rows_used']} рядках із {m['n_rows_total']}")
    print(f"Від 22 °C до 18 °C при −10 °C надворі: "
          f"{m['hours_to_18_from_22_at_minus10']:.0f} год "
          f"({m['cooling_rate_c_per_h_at_minus10']:.2f} °C/год на старті)")


if __name__ == "__main__":
    main()
