"""Модуль M6 — детекція готовки на кухні за вологістю (проєкт «4331 година»).

Ідея: коли готують, вологість на кухні різко піднімається над своєю ж
нещодавньою базою. Порівнюємо зі спальнею в ту саму хвилину, щоб відрізнити
локальний стрибок (готовка) від спільного (волога погода).

Два незалежні методи на двох джерелах даних:
  * `detect_events_detailed` — 1-хв ряд за 10 днів (`readings_detailed.csv`),
    база = ковзна медіана попередньої години, поріг X п.п., подія
    завершується поверненням до бази ±2 п.п. або через 3 год.
  * `detect_events_hourly` — погодинний ряд за 6 місяців (`hours_wide.csv`),
    приріст = kitchen_hum − AVG(kitchen_hum) за 3 попередні години
    (`ROWS BETWEEN 3 PRECEDING AND 1 PRECEDING`, як у SQL), поріг Y п.п.
    Це правило зроблено відтворюваним у `sql/05_cooking.sql` віконною функцією.

Пороги підібрані на даних (перевірено очима на графіку `m6_example_week.png`,
таблиця вибору — `tune_threshold_detailed`), Y узгоджений з X.

Запуск: `PYTHONUTF8=1 python -m src.cooking`
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

KYIV = "Europe/Kyiv"

KITCHEN_HUM_ENTITY = "sensor.test_product_humidity_2"
BEDROOM_HUM_ENTITY = "sensor.test_product_humidity"

# ── Параметри детекції ────────────────────────────────────────────────────────
THRESHOLD_PP_DETAILED = 8.0   # X, п.п. — поріг приросту на 1-хв даних
THRESHOLD_PP_HOURLY = 6.0     # Y, п.п. — поріг приросту на погодинних даних
BASELINE_WINDOW_MIN = 60      # медіана попередньої години (1-хв дані)
RETURN_WITHIN_PP = 2.0        # подія закінчується, коли вологість в межах бази +2 п.п.
MAX_EVENT_HOURS = 3           # примусове завершення події через 3 год
MERGE_GAP_MIN = 15            # склеюємо сусідні події, якщо розрив < 15 хв
HOURLY_WINDOW_H = 3           # AVG за 3 попередні години (без поточної)
TUNE_THRESHOLDS = (5.0, 8.0, 10.0)
EVENING_HOURS = range(17, 23)  # 17:00–22:59 Київ — «вечірній» проміжок


# ── 1. Завантаження ───────────────────────────────────────────────────────────
def load_detailed(root: str | Path) -> pd.DataFrame:
    """1-хвилинний ряд кухонної й спальної вологості з `readings_detailed.csv`.

    Детальна історія пише «за подією» (кожні ~10–20 с) — ресемплимо до 1 хв
    середнім і заповнюємо лише короткі (≤5 хв) прогалини, щоб не тягнути
    штучний тренд крізь справжні дірки.
    """
    df = pd.read_csv(Path(root) / "data" / "clean" / "readings_detailed.csv")
    df["ts"] = pd.to_datetime(df["ts"], utc=True, format="mixed")

    def _series(entity: str) -> pd.Series:
        s = (df.loc[df["entity_id"].eq(entity), ["ts", "value"]]
               .set_index("ts")["value"].sort_index())
        return s.resample("1min").mean().interpolate(limit=5)

    out = pd.DataFrame({
        "kitchen_hum": _series(KITCHEN_HUM_ENTITY),
        "bedroom_hum": _series(BEDROOM_HUM_ENTITY),
    })
    return out


def load_hourly(root: str | Path) -> pd.DataFrame:
    """Погодинний ряд `ts`, `ts_kyiv`, `kitchen_hum` із `hours_wide.csv`.

    Пропуски кухонної вологості (68 % покриття) НЕ заповнюємо — вони мають
    залишитись `NaN` і випасти з детекції, а не підмінятись значенням.
    """
    wide = pd.read_csv(Path(root) / "data" / "clean" / "hours_wide.csv",
                       usecols=["ts", "ts_kyiv", "kitchen_hum"])
    wide["ts"] = pd.to_datetime(wide["ts"], utc=True, format="mixed")
    wide["ts_kyiv"] = pd.to_datetime(wide["ts_kyiv"], utc=True, format="mixed").dt.tz_convert(KYIV)
    return wide.sort_values("ts").reset_index(drop=True)


def kitchen_coverage_pct(root: str | Path) -> float:
    """Покриття кухонної вологості (%) — читаємо з результату M1, не рахуємо заново."""
    m1_path = Path(root) / "results" / "m1_quality.json"
    if m1_path.exists():
        m1 = json.loads(m1_path.read_text(encoding="utf-8"))
        if "coverage_kitchen_hum" in m1:
            return float(m1["coverage_kitchen_hum"])
    wide = load_hourly(root)
    return round(wide["kitchen_hum"].notna().mean() * 100, 1)


# ── 2. Детекція на 1-хв даних (10 днів) ───────────────────────────────────────
def _detect_raw_detailed(hum: pd.Series, threshold_pp: float) -> list[dict]:
    """Стан-машина: подія стартує при приросту ≥ threshold_pp над базою,
    закінчується поверненням у межі бази ±`RETURN_WITHIN_PP` або через
    `MAX_EVENT_HOURS`. Повертає список подій до склеювання."""
    baseline = hum.rolling(f"{BASELINE_WINDOW_MIN}min", closed="left").median()
    rise = hum - baseline
    idx = hum.index
    n = len(hum)
    events, in_event = [], False
    start_i = pre_baseline = None
    max_min = MAX_EVENT_HOURS * 60

    for i in range(n):
        if not in_event:
            if pd.notna(rise.iloc[i]) and rise.iloc[i] >= threshold_pp:
                start_i, pre_baseline, in_event = i, baseline.iloc[i], True
        else:
            elapsed = (idx[i] - idx[start_i]).total_seconds() / 60
            returned = pd.notna(hum.iloc[i]) and hum.iloc[i] <= pre_baseline + RETURN_WITHIN_PP
            if returned or elapsed >= max_min:
                seg = hum.iloc[start_i:i + 1]
                peak_pos = int(np.nanargmax(seg.to_numpy()))
                events.append({
                    "start_utc": idx[start_i], "end_utc": idx[i],
                    "baseline_hum": float(pre_baseline), "peak_hum": float(seg.max()),
                    "peak_utc": seg.index[peak_pos], "returned": bool(returned),
                })
                in_event = False
    if in_event:
        seg = hum.iloc[start_i:]
        peak_pos = int(np.nanargmax(seg.to_numpy()))
        events.append({
            "start_utc": idx[start_i], "end_utc": idx[-1],
            "baseline_hum": float(pre_baseline), "peak_hum": float(seg.max()),
            "peak_utc": seg.index[peak_pos], "returned": False,
        })
    return events


def _merge_close(events: list[dict], gap_min: float = MERGE_GAP_MIN) -> list[dict]:
    """Склеює послідовні події, якщо розрив між кінцем однієї й початком наступної < gap_min."""
    merged: list[dict] = []
    for ev in events:
        if merged and (ev["start_utc"] - merged[-1]["end_utc"]).total_seconds() / 60 < gap_min:
            prev = merged[-1]
            prev["end_utc"] = ev["end_utc"]
            prev["returned"] = ev["returned"]
            if ev["peak_hum"] > prev["peak_hum"]:
                prev["peak_hum"], prev["peak_utc"] = ev["peak_hum"], ev["peak_utc"]
            prev["baseline_hum"] = min(prev["baseline_hum"], ev["baseline_hum"])
        else:
            merged.append(dict(ev))
    return merged


def detect_events_detailed(minute_df: pd.DataFrame,
                           threshold_pp: float = THRESHOLD_PP_DETAILED) -> pd.DataFrame:
    """Події готовки на 1-хв даних. Повертає таблицю з приростом, тривалістю,
    часом повернення до бази й порівнянням зі спальнею (локальний стрибок чи ні)."""
    raw = _detect_raw_detailed(minute_df["kitchen_hum"], threshold_pp)
    merged = _merge_close(raw)
    if not merged:
        cols = ["start_utc", "end_utc", "duration_min", "baseline_hum", "peak_hum",
                "rise_pp", "return_min", "returned", "bedroom_rise_pp", "is_local"]
        return pd.DataFrame(columns=cols)

    bh = minute_df["bedroom_hum"]
    rows = []
    for ev in merged:
        duration_min = (ev["end_utc"] - ev["start_utc"]).total_seconds() / 60
        return_min = ((ev["end_utc"] - ev["peak_utc"]).total_seconds() / 60
                      if ev["returned"] else np.nan)
        b_seg = bh.loc[ev["start_utc"]:ev["end_utc"]]
        bedroom_rise = float(b_seg.max() - b_seg.iloc[0]) if len(b_seg) else np.nan
        rise_pp = ev["peak_hum"] - ev["baseline_hum"]
        rows.append({
            "start_utc": ev["start_utc"], "end_utc": ev["end_utc"],
            "duration_min": round(duration_min, 1),
            "baseline_hum": round(ev["baseline_hum"], 2), "peak_hum": round(ev["peak_hum"], 2),
            "rise_pp": round(rise_pp, 2),
            "return_min": round(return_min, 1) if pd.notna(return_min) else np.nan,
            "returned": ev["returned"],
            "bedroom_rise_pp": round(bedroom_rise, 2) if pd.notna(bedroom_rise) else np.nan,
            "is_local": bool(pd.notna(bedroom_rise) and bedroom_rise < rise_pp / 2),
        })
    return pd.DataFrame(rows).sort_values("start_utc").reset_index(drop=True)


def tune_threshold_detailed(minute_df: pd.DataFrame,
                            thresholds=TUNE_THRESHOLDS) -> pd.DataFrame:
    """Скільки подій дає кожен кандидат-поріг X — таблиця для вибору на графіку."""
    span_days = (minute_df.index.max() - minute_df.index.min()).total_seconds() / 86400
    rows = []
    for thr in thresholds:
        ev = detect_events_detailed(minute_df, threshold_pp=thr)
        rows.append({
            "threshold_pp": thr, "n_events": len(ev),
            "events_per_week": round(len(ev) / (span_days / 7), 2) if span_days else np.nan,
            "median_duration_min": round(ev["duration_min"].median(), 1) if len(ev) else np.nan,
            "n_local": int(ev["is_local"].sum()) if len(ev) else 0,
        })
    return pd.DataFrame(rows)


# ── 3. Детекція на погодинних даних (6 місяців, SQL-відтворювана) ────────────
def detect_events_hourly(wide: pd.DataFrame,
                         threshold_pp: float = THRESHOLD_PP_HOURLY,
                         window_h: int = HOURLY_WINDOW_H) -> pd.DataFrame:
    """Погодинне правило: приріст = kitchen_hum − AVG(kitchen_hum) за
    `window_h` попередніх годин (SQL: `ROWS BETWEEN window_h PRECEDING AND 1 PRECEDING`,
    `AVG` ігнорує `NULL`). Години з `NaN` kitchen_hum пропускаються, не заповнюються.
    Послідовні позначені години склеюються в одну подію (gaps-and-islands)."""
    kh = wide["kitchen_hum"]
    prev_avg = kh.shift(1).rolling(window_h, min_periods=1).mean()
    rise = kh - prev_avg
    flag = rise.ge(threshold_pp) & kh.notna()

    cols = ["start_utc", "end_utc", "duration_min", "baseline_hum", "peak_hum",
            "rise_pp", "start_kyiv", "n_hours"]
    if not flag.any():
        return pd.DataFrame(columns=cols)

    grp = (flag != flag.shift()).cumsum()
    rows = []
    for _, idx in wide.loc[flag].groupby(grp[flag]).groups.items():
        idx = list(idx)
        seg_hum = kh.loc[idx]
        start_ts, end_ts = wide.loc[idx[0], "ts"], wide.loc[idx[-1], "ts"] + pd.Timedelta(hours=1)
        rows.append({
            "start_utc": start_ts, "end_utc": end_ts,
            "duration_min": (end_ts - start_ts).total_seconds() / 60,
            "baseline_hum": round(float(prev_avg.loc[idx[0]]), 2),
            "peak_hum": round(float(seg_hum.max()), 2),
            "rise_pp": round(float(rise.loc[idx].max()), 2),
            "start_kyiv": wide.loc[idx[0], "ts_kyiv"],
            "n_hours": len(idx),
        })
    return pd.DataFrame(rows).sort_values("start_utc").reset_index(drop=True)


# ── 4. Спільна таблиця подій + метрики ────────────────────────────────────────
def _add_kyiv_fields(events: pd.DataFrame, start_kyiv_col: str | None = None) -> pd.DataFrame:
    ev = events.copy()
    if start_kyiv_col is None:
        ev["start_kyiv"] = ev["start_utc"].dt.tz_convert(KYIV)
    ev["hour_kyiv"] = ev["start_kyiv"].dt.hour
    ev["weekday"] = ev["start_kyiv"].dt.weekday  # 0 = понеділок
    return ev


def build_event_table(detailed_events: pd.DataFrame, hourly_events: pd.DataFrame) -> pd.DataFrame:
    """Об'єднана таблиця подій обох джерел — `results/m6_cooking_events.csv`."""
    d = _add_kyiv_fields(detailed_events) if len(detailed_events) else detailed_events.copy()
    if len(d):
        d = d.assign(source="detailed")
    h = _add_kyiv_fields(hourly_events, start_kyiv_col="start_kyiv") if len(hourly_events) else hourly_events.copy()
    if len(h):
        h = h.assign(source="hourly")

    parts = [x for x in (d, h) if len(x)]
    out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if out.empty:
        return pd.DataFrame(columns=["event_id", "source", "start_utc", "end_utc", "start_kyiv",
                                     "duration_min", "rise_pp", "peak_hum", "baseline_hum",
                                     "hour_kyiv", "weekday"])
    out = out.sort_values(["source", "start_utc"]).reset_index(drop=True)
    seq = out.groupby("source").cumcount() + 1
    out.insert(0, "event_id", [f"{s[:1]}{n:03d}" for s, n in zip(out["source"], seq)])
    return out[["event_id", "source", "start_utc", "end_utc", "start_kyiv", "duration_min",
               "rise_pp", "peak_hum", "baseline_hum", "hour_kyiv", "weekday"]]


def compute_metrics(detailed_events: pd.DataFrame, hourly_events: pd.DataFrame,
                    detailed_span_days: float, hourly_span_days: float,
                    kitchen_cov_pct: float) -> dict:
    """Флет-словник для `results/m6_cooking.json`.

    Приріст/тривалість/повернення — з детальних (1-хв) подій, де вони точні;
    година доби й вечірня частка — з погодинних (6 міс, більша вибірка).
    """
    d, h = detailed_events, _add_kyiv_fields(hourly_events, start_kyiv_col="start_kyiv") if len(hourly_events) else hourly_events

    def _safe(fn, default=np.nan):
        try:
            v = fn()
            return None if pd.isna(v) else round(float(v), 2)
        except (ValueError, KeyError):
            return default

    hour_counts = h["hour_kyiv"].value_counts() if len(h) else pd.Series(dtype=int)
    peak_hour = int(hour_counts.idxmax()) if len(hour_counts) else None
    share_evening = (h["hour_kyiv"].isin(EVENING_HOURS).mean() * 100) if len(h) else np.nan

    return {
        "threshold_pp_detailed": THRESHOLD_PP_DETAILED,
        "threshold_pp_hourly": THRESHOLD_PP_HOURLY,
        "baseline_window_min": BASELINE_WINDOW_MIN,
        "hourly_window_h": HOURLY_WINDOW_H,
        "n_events_detailed": int(len(d)),
        "events_per_week_detailed": round(len(d) / (detailed_span_days / 7), 2) if detailed_span_days else None,
        "n_events_hourly": int(len(h)),
        "events_per_week_hourly": round(len(h) / (hourly_span_days / 7), 2) if hourly_span_days else None,
        "mean_rise_pp": _safe(lambda: d["rise_pp"].mean()),
        "median_rise_pp": _safe(lambda: d["rise_pp"].median()),
        "mean_duration_min": _safe(lambda: d["duration_min"].mean()),
        "mean_return_min": _safe(lambda: d["return_min"].mean()),
        "peak_hour_kyiv": peak_hour,
        "share_evening_pct": None if pd.isna(share_evening) else round(float(share_evening), 1),
        "kitchen_coverage_pct": float(kitchen_cov_pct),
    }


# ── 5. Усе разом ──────────────────────────────────────────────────────────────
def run_all(root: str | Path = ".") -> dict:
    """Проганяє весь M6: детекція на обох джерелах, події + метрики, запис файлів.

    Пише `results/m6_cooking_events.csv`, `results/m6_cooking.json`.
    Повертає словник із проміжними таблицями для нотбука (`minute`, `wide`,
    `detailed_events`, `hourly_events`, `tuning`, `events`, `metrics`).
    """
    root = Path(root)
    res_dir = root / "results"
    res_dir.mkdir(parents=True, exist_ok=True)

    minute = load_detailed(root)
    wide = load_hourly(root)
    cov = kitchen_coverage_pct(root)

    tuning = tune_threshold_detailed(minute)
    detailed_events = detect_events_detailed(minute, threshold_pp=THRESHOLD_PP_DETAILED)
    hourly_events = detect_events_hourly(wide, threshold_pp=THRESHOLD_PP_HOURLY)

    detailed_span = (minute.index.max() - minute.index.min()).total_seconds() / 86400
    hourly_span = (wide["ts"].max() - wide["ts"].min()).total_seconds() / 86400

    events = build_event_table(detailed_events, hourly_events)
    metrics = compute_metrics(detailed_events, hourly_events, detailed_span, hourly_span, cov)

    events.to_csv(res_dir / "m6_cooking_events.csv", index=False)
    (res_dir / "m6_cooking.json").write_text(
        json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "minute": minute, "wide": wide, "tuning": tuning,
        "detailed_events": detailed_events, "hourly_events": hourly_events,
        "events": events, "metrics": metrics,
    }


if __name__ == "__main__":
    out = run_all(Path(__file__).resolve().parent.parent)
    print(json.dumps(out["metrics"], indent=2, ensure_ascii=False))
