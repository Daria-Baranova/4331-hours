"""Модуль M1 — якість даних і чищення (проєкт «4331 година»).

Перетворює сирі вивантаження з Home Assistant у чисті таблиці `data/clean/`,
з якими працюють усі наступні модулі.

Що робимо (перевірки зі специфікації, розділ M1):
  1. нечислові стани `unknown` / `unavailable` — прибрати й порахувати;
  2. дублікати `(ts, entity_id)` — лишити останній;
  3. «застряглий» датчик — однакове значення ≥ 6 годин поспіль — позначити прапорцем;
  4. викиди поза фізичним діапазоном — у NaN;
  5. різна частота записів — ресемпл до повного погодинного календаря;
  6. часовий пояс — усе рахуємо в UTC, у Europe/Kyiv переводимо лише для людини;
  7. перехід на літній час — ловиться сам, бо працюємо в UTC;
  8. пропущені години — окрема таблиця «дірок».

Запуск: `PYTHONUTF8=1 python -m src.cleaning`
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from .ha_client import ROOMS_UA, SENSORS, TECH

# ── Довідники ─────────────────────────────────────────────────────────────────

#: Метрика для технічних датчиків (M5): entity_id -> коротка назва метрики.
TECH_METRIC: dict[str, str] = {
    "sensor.nas_disk_sda_temperature": "nas_temp",
    "sensor.nas_disk_sdb_temperature": "nas_temp",
    "sensor.nas_nvme_cache_temperature": "nas_temp",
    "sensor.192_168_0_153_cpu_load": "cpu_load",
    "sensor.pc_cpu_power": "pc_power",
    "sensor.pc_cpu_temperature": "pc_temp",
}

#: Фізично можливі діапазони. Усе поза ними — помилка датчика, а не факт.
RANGES: dict[str, tuple[float, float]] = {
    "temperature": (-20.0, 60.0),
    "humidity": (0.0, 100.0),
    "illuminance": (0.0, np.inf),
}

#: entity_id -> назва колонки в широкій таблиці hours_wide.
WIDE_COLS: dict[str, str] = {
    "sensor.test_product_temperature": "bedroom_temp",
    "sensor.test_product_humidity": "bedroom_hum",
    "sensor.test_product_illuminance": "bedroom_lux",
    "sensor.test_product_temperature_2": "kitchen_temp",
    "sensor.test_product_humidity_2": "kitchen_hum",
    "sensor.test_product_temperature_3": "printernya_temp",
    "sensor.test_product_humidity_3": "printernya_hum",
    "sensor.tiny_soil_sensor_temperature": "soil_temp",
    "sensor.nas_disk_sda_temperature": "nas_sda_temp",
    "sensor.nas_disk_sdb_temperature": "nas_sdb_temp",
    "sensor.nas_nvme_cache_temperature": "nas_nvme_temp",
    "sensor.192_168_0_153_cpu_load": "server_cpu_load",
    "sensor.pc_cpu_power": "pc_power",
    "sensor.pc_cpu_temperature": "pc_temp",
}

#: Порядок колонок hours_wide — контракт для всіх наступних модулів.
WIDE_ORDER: list[str] = [
    "ts", "ts_kyiv",
    "bedroom_temp", "bedroom_hum", "bedroom_lux",
    "kitchen_temp", "kitchen_hum",
    "printernya_temp", "printernya_hum",
    "soil_temp",
    "out_temp", "out_hum", "out_wind", "out_cloud",
    "nas_sda_temp", "nas_sdb_temp", "nas_nvme_temp",
    "server_cpu_load", "pc_power", "pc_temp",
]

#: Українські підписи датчиків для графіків.
LABELS_UA: dict[str, str] = {
    "sensor.test_product_temperature": "кімната · температура",
    "sensor.test_product_humidity": "кімната · вологість",
    "sensor.test_product_illuminance": "кімната · освітленість",
    "sensor.test_product_temperature_2": "кухня · температура",
    "sensor.test_product_humidity_2": "кухня · вологість",
    "sensor.test_product_temperature_3": "кладова · температура",
    "sensor.test_product_humidity_3": "кладова · вологість",
    "sensor.tiny_soil_sensor_temperature": "квітка · температура (батарейка)",
    "sensor.nas_disk_sda_temperature": "NAS диск sda",
    "sensor.nas_disk_sdb_temperature": "NAS диск sdb",
    "sensor.nas_nvme_cache_temperature": "NAS NVMe",
    "sensor.192_168_0_153_cpu_load": "сервер · завантаження CPU",
    "sensor.pc_cpu_power": "ПК · потужність",
    "sensor.pc_cpu_temperature": "ПК · температура",
}

#: Палітра кімнат (єдина для всього проєкту).
ROOM_COLORS: dict[str, str] = {
    "Bedroom": "#2E86AB",
    "Kitchen": "#F18F01",
    "Printernya": "#C73E1D",
    "Soil": "#4C956C",
    "Tech": "#6C757D",
}

#: Колір окремого датчика — відтінок кольору своєї кімнати.
ENTITY_COLORS: dict[str, str] = {
    "sensor.test_product_temperature": "#2E86AB",
    "sensor.test_product_humidity": "#5BA3C4",
    "sensor.test_product_illuminance": "#93C4DC",
    "sensor.test_product_temperature_2": "#F18F01",
    "sensor.test_product_humidity_2": "#F7B85C",
    "sensor.test_product_temperature_3": "#C73E1D",
    "sensor.test_product_humidity_3": "#E0765C",
    "sensor.tiny_soil_sensor_temperature": "#4C956C",
    "sensor.nas_disk_sda_temperature": "#6C757D",
    "sensor.nas_disk_sdb_temperature": "#868E96",
    "sensor.nas_nvme_cache_temperature": "#A0A7AD",
    "sensor.192_168_0_153_cpu_load": "#495057",
    "sensor.pc_cpu_power": "#343A40",
    "sensor.pc_cpu_temperature": "#5A6268",
}

ROOM_LABELS_UA: dict[str, str] = {
    **ROOMS_UA,
    "Soil": "датчик квітки (контроль)",
    "Tech": "техніка",
}

KYIV = "Europe/Kyiv"
STUCK_HOURS = 6
KNOWN = set(SENSORS) | set(TECH)


# ── Допоміжні ─────────────────────────────────────────────────────────────────
def _room_of(entity_id: str) -> str:
    """Кімната датчика: із реєстру SENSORS, «Tech» для техніки, «Soil» для квітки."""
    if entity_id in SENSORS:
        room = SENSORS[entity_id][0]
        return room if room else "Soil"
    return "Tech"


def _metric_of(entity_id: str) -> str:
    """Метрика датчика: із реєстру SENSORS або з мапи TECH_METRIC."""
    if entity_id in SENSORS:
        return SENSORS[entity_id][1]
    return TECH_METRIC.get(entity_id, "other")


def _out_of_range_mask(values: pd.Series, metric: pd.Series) -> pd.Series:
    """True там, де значення вийшло за фізичний діапазон своєї метрики."""
    mask = pd.Series(False, index=values.index)
    for name, (lo, hi) in RANGES.items():
        sel = metric.eq(name) & values.notna()
        mask |= sel & (values.lt(lo) | values.gt(hi))
    return mask


def _stuck_runs(values: np.ndarray, n: int = STUCK_HOURS) -> np.ndarray:
    """Прапорець «датчик застряг»: однакове значення ≥ n годин поспіль (NaN рве серію)."""
    if len(values) == 0:
        return np.zeros(0, dtype=bool)
    isna = pd.isna(values)
    new_run = np.ones(len(values), dtype=bool)
    if len(values) > 1:
        same = (values[1:] == values[:-1]) & ~isna[1:] & ~isna[:-1]
        new_run[1:] = ~same
    run_id = np.cumsum(new_run)
    sizes = np.bincount(run_id)[run_id]
    return (sizes >= n) & ~isna


# ── 1. Завантаження ───────────────────────────────────────────────────────────
def load_raw(root: str | Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Читає три сирі файли з `data/raw/`; `ts` парситься як UTC.

    Повертає `(stats, history, weather)`.
    """
    raw = Path(root) / "data" / "raw"
    stats = pd.read_csv(raw / "stats_hourly.csv")
    stats["ts"] = pd.to_datetime(stats["ts"], utc=True, format="mixed")

    history = pd.read_csv(raw / "history_10d.csv")
    history["ts"] = pd.to_datetime(history["ts"], utc=True, format="mixed")

    weather = pd.read_csv(raw / "weather.csv")
    weather["ts"] = pd.to_datetime(weather["ts"], utc=True, format="mixed")
    weather = weather[["ts", "out_temp", "out_hum", "out_wind", "out_cloud"]]
    return stats, history, weather


# ── 2. Погодинна статистика ───────────────────────────────────────────────────
def clean_hourly(stats: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Чистить погодинну статистику (6 місяців) і розкладає її на повний календар.

    Кроки: лишаємо лише датчики з реєстру → додаємо `room`/`metric` → викиди в NaN →
    прибираємо дублікати `(ts, entity_id)` (лишаємо останній) → кожен датчик
    ресемплимо на суцільний погодинний календар від його першого запису до
    глобального максимуму (NaN там, де запису не було) → прапорець `stuck`.

    Повертає `(hourly, stats_log)`, де `stats_log` — лічильники на датчик
    (`duplicates`, `out_of_range`, `raw_rows`) для таблиці «до / після».
    """
    df = stats.copy()
    raw_counts = df.groupby("entity_id").size().rename("raw_rows")

    df = df[df["entity_id"].isin(KNOWN)].copy()
    df["ts"] = df["ts"].dt.floor("h")
    df["room"] = df["entity_id"].map(_room_of)
    df["metric"] = df["entity_id"].map(_metric_of)

    # 4. Викиди поза фізичним діапазоном -> NaN
    oor = pd.Series(False, index=df.index)
    for col in ("mean", "min", "max"):
        m = _out_of_range_mask(df[col], df["metric"])
        df.loc[m, col] = np.nan
        oor |= m
    oor_counts = oor.groupby(df["entity_id"]).sum().rename("out_of_range")

    # 2. Дублікати (ts, entity_id) -> лишаємо останній
    dup = df.duplicated(["ts", "entity_id"], keep="last")
    dup_counts = dup.groupby(df["entity_id"]).sum().rename("duplicates")
    df = df[~dup]

    # 5. Ресемпл на повний погодинний календар
    global_max = df["ts"].max()
    parts = []
    for eid, g in df.sort_values("ts").groupby("entity_id", sort=True):
        cal = pd.date_range(g["ts"].min(), global_max, freq="h", tz="UTC")
        g = g.set_index("ts").reindex(cal)
        g.index.name = "ts"
        g["entity_id"] = eid
        g["room"] = _room_of(eid)
        g["metric"] = _metric_of(eid)
        # 3. «Застряглий» датчик — лише для температури/вологості
        if g["metric"].iloc[0] in ("temperature", "humidity"):
            g["stuck"] = _stuck_runs(g["mean"].to_numpy())
        else:
            g["stuck"] = False
        parts.append(g.reset_index())

    hourly = pd.concat(parts, ignore_index=True)
    hourly = hourly[["ts", "entity_id", "room", "metric", "mean", "min", "max", "stuck"]]
    hourly = hourly.sort_values(["entity_id", "ts"]).reset_index(drop=True)

    stats_log = pd.concat([raw_counts, dup_counts, oor_counts], axis=1).fillna(0).astype(int)
    return hourly, stats_log


# ── 3. Детальна історія ───────────────────────────────────────────────────────
def clean_detailed(history: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Чистить детальну історію (10 днів, ~14 с) — лише 8 датчиків квартири.

    Кроки: лишаємо лише `SENSORS` → рахуємо й прибираємо нечислові стани
    (`unknown` / `unavailable`) → `float` → викиди в NaN → дублікати
    `(ts, entity_id)` (лишаємо останній) → `room`/`metric`.

    Повертає `(detailed, detailed_log)` з лічильниками на датчик.
    """
    df = history[history["entity_id"].isin(SENSORS)].copy()
    raw_counts = df.groupby("entity_id").size().rename("raw_rows_detailed")

    # 1. Нечислові стани
    value = pd.to_numeric(df["state"], errors="coerce")
    bad = value.isna()
    nn_counts = bad.groupby(df["entity_id"]).sum().rename("non_numeric")
    df = df[~bad].copy()
    df["value"] = value[~bad].astype(float)

    df["room"] = df["entity_id"].map(_room_of)
    df["metric"] = df["entity_id"].map(_metric_of)

    # 4. Викиди
    oor = _out_of_range_mask(df["value"], df["metric"])
    oor_counts = oor.groupby(df["entity_id"]).sum().rename("out_of_range_detailed")
    df.loc[oor, "value"] = np.nan

    # 2. Дублікати
    dup = df.duplicated(["ts", "entity_id"], keep="last")
    dup_counts = dup.groupby(df["entity_id"]).sum().rename("duplicates_detailed")
    df = df[~dup]

    detailed = (df[["ts", "entity_id", "room", "metric", "value"]]
                .sort_values(["entity_id", "ts"]).reset_index(drop=True))

    detailed_log = pd.concat([raw_counts, nn_counts, oor_counts, dup_counts],
                             axis=1).fillna(0).astype(int)
    return detailed, detailed_log


# ── 4. Дірки ──────────────────────────────────────────────────────────────────
def find_gaps(hourly: pd.DataFrame, min_hours: int = 1) -> pd.DataFrame:
    """Таблиця «дірок»: проміжки між сусідніми наявними годинами > `min_hours`.

    `gap_start` — остання година із записом перед діркою, `gap_end` — перша
    година із записом після неї, `hours` — скільки годин пропущено між ними.
    """
    rows = []
    present = hourly[hourly["mean"].notna()]
    for eid, g in present.groupby("entity_id", sort=True):
        ts = g["ts"].sort_values().reset_index(drop=True)
        if len(ts) < 2:
            continue
        diff_h = ts.diff().dt.total_seconds().div(3600)
        idx = diff_h[diff_h > min_hours].index
        for i in idx:
            rows.append({"entity_id": eid,
                         "gap_start": ts[i - 1],
                         "gap_end": ts[i],
                         "hours": int(round(diff_h[i])) - 1})
    gaps = pd.DataFrame(rows, columns=["entity_id", "gap_start", "gap_end", "hours"])
    return gaps.sort_values(["entity_id", "gap_start"]).reset_index(drop=True)


def stuck_runs(hourly: pd.DataFrame) -> pd.DataFrame:
    """Періоди «застрягання»: суцільні серії з прапорцем `stuck` (entity_id, start, end, value, hours)."""
    s = hourly[hourly["stuck"]].sort_values(["entity_id", "ts"]).copy()
    if s.empty:
        return pd.DataFrame(columns=["entity_id", "start", "end", "value", "hours"])
    new = (s["entity_id"] != s["entity_id"].shift()) | (s["ts"].diff() != pd.Timedelta("1h"))
    s["run"] = new.cumsum()
    out = s.groupby("run").agg(entity_id=("entity_id", "first"), start=("ts", "min"),
                               end=("ts", "max"), value=("mean", "first"), hours=("ts", "size"))
    return out.reset_index(drop=True)


# ── 5. Широка таблиця ─────────────────────────────────────────────────────────
def build_hours_wide(hourly: pd.DataFrame, weather: pd.DataFrame) -> pd.DataFrame:
    """Головна аналітична таблиця: один рядок на кожну годину календаря.

    Беремо `mean` кожного датчика, розкладаємо по колонках `WIDE_ORDER`,
    приєднуємо погоду по UTC і додаємо київський час `ts_kyiv`.
    """
    wide = hourly.pivot_table(index="ts", columns="entity_id", values="mean", aggfunc="last")
    wide = wide.rename(columns=WIDE_COLS)

    cal = pd.date_range(hourly["ts"].min(), hourly["ts"].max(), freq="h", tz="UTC")
    wide = wide.reindex(cal)
    wide.index.name = "ts"
    wide = wide.reset_index()

    wide = wide.merge(weather, on="ts", how="left")
    # 6. У київський час переводимо лише для людини, join лишається в UTC
    wide["ts_kyiv"] = wide["ts"].dt.tz_convert(KYIV)

    for col in WIDE_ORDER:
        if col not in wide.columns:
            wide[col] = np.nan
    return wide[WIDE_ORDER].sort_values("ts").reset_index(drop=True)


# ── 6. Звіт «до / після» ──────────────────────────────────────────────────────
def quality_report(hourly: pd.DataFrame, stats_log: pd.DataFrame,
                   detailed_log: pd.DataFrame, gaps: pd.DataFrame) -> pd.DataFrame:
    """Таблиця «до / після» по кожному датчику — головний результат M1.

    Колонки: `raw_rows`, `non_numeric` (лише детальна історія), `duplicates`,
    `out_of_range`, `stuck_hours`, `calendar_hours`, `present_hours`,
    `coverage_pct`, `gaps_gt_1h`, `dropped_pct`.
    """
    agg = hourly.groupby("entity_id").agg(
        room=("room", "first"),
        metric=("metric", "first"),
        calendar_hours=("ts", "size"),
        present_hours=("mean", "count"),
        stuck_hours=("stuck", "sum"),
        first_ts=("ts", "min"),
    )
    rep = agg.join(stats_log, how="left").join(detailed_log, how="left")
    rep["non_numeric"] = rep.get("non_numeric", pd.Series(dtype=float)).fillna(0)
    for col in ("raw_rows", "duplicates", "out_of_range", "non_numeric",
                "raw_rows_detailed", "duplicates_detailed", "out_of_range_detailed"):
        if col not in rep.columns:
            rep[col] = 0
        rep[col] = rep[col].fillna(0).astype(int)

    rep["gaps_gt_1h"] = rep.index.map(gaps.groupby("entity_id").size()).fillna(0).astype(int)
    rep["coverage_pct"] = (rep["present_hours"] / rep["calendar_hours"] * 100).round(1)
    dropped = rep["duplicates"] + rep["out_of_range"]
    rep["dropped_pct"] = (dropped / rep["raw_rows"].where(rep["raw_rows"] > 0) * 100).fillna(0).round(2)
    rep["label_ua"] = rep.index.map(LABELS_UA)
    rep["stuck_hours"] = rep["stuck_hours"].astype(int)

    cols = ["label_ua", "room", "metric", "first_ts", "raw_rows", "non_numeric",
            "duplicates", "out_of_range", "stuck_hours", "calendar_hours",
            "present_hours", "coverage_pct", "gaps_gt_1h", "dropped_pct"]
    return rep[cols].sort_values(["room", "metric"]).reset_index()


# ── 7. Усе разом ──────────────────────────────────────────────────────────────
def run_all(root: str | Path = ".") -> dict:
    """Проганяє весь M1 і записує чисті файли, таблицю якості та JSON з підсумками.

    Пише: `data/clean/{readings_hourly,readings_detailed,weather_hourly,hours_wide,gaps}.csv`,
    `results/m1_quality.csv`, `results/m1_quality.json`.
    """
    root = Path(root)
    clean_dir, res_dir = root / "data" / "clean", root / "results"
    clean_dir.mkdir(parents=True, exist_ok=True)
    res_dir.mkdir(parents=True, exist_ok=True)

    stats, history, weather = load_raw(root)
    hourly, stats_log = clean_hourly(stats)
    detailed, detailed_log = clean_detailed(history)
    gaps = find_gaps(hourly, min_hours=1)
    stuck = stuck_runs(hourly)
    wide = build_hours_wide(hourly, weather)
    report = quality_report(hourly, stats_log, detailed_log, gaps)

    hourly.to_csv(clean_dir / "readings_hourly.csv", index=False)
    detailed.to_csv(clean_dir / "readings_detailed.csv", index=False)
    weather.to_csv(clean_dir / "weather_hourly.csv", index=False)
    wide.to_csv(clean_dir / "hours_wide.csv", index=False)
    gaps.to_csv(clean_dir / "gaps.csv", index=False)
    report.to_csv(res_dir / "m1_quality.csv", index=False)

    rooms = report[report["room"].isin(ROOMS_UA)]
    totals = {
        "total_hours": int(len(wide)),
        "sensors": int(report["entity_id"].nunique()),
        "overall_coverage_pct": round(
            report["present_hours"].sum() / report["calendar_hours"].sum() * 100, 1),
        "overall_coverage_rooms_pct": round(
            rooms["present_hours"].sum() / rooms["calendar_hours"].sum() * 100, 1),
        "n_gaps_gt_1h": int(len(gaps)),
        "longest_gap_hours": int(gaps["hours"].max()) if len(gaps) else 0,
        "non_numeric_rows": int(report["non_numeric"].sum()),
        "duplicates": int(stats_log["duplicates"].sum() + detailed_log["duplicates_detailed"].sum()),
        "out_of_range": int(stats_log["out_of_range"].sum()
                            + detailed_log["out_of_range_detailed"].sum()),
        "stuck_hours": int(report["stuck_hours"].sum()),
        "n_stuck_runs": int(len(stuck)),
        "longest_stuck_hours": int(stuck["hours"].max()) if len(stuck) else 0,
        "detailed_rows": int(len(detailed)),
        "hourly_rows": int(len(hourly)),
        "ts_start_utc": str(wide["ts"].min()),
        "ts_end_utc": str(wide["ts"].max()),
    }
    for eid, cov in zip(report["entity_id"], report["coverage_pct"]):
        totals[f"coverage_{WIDE_COLS[eid]}"] = float(cov)

    (res_dir / "m1_quality.json").write_text(
        json.dumps(totals, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"hourly": hourly, "detailed": detailed, "weather": weather, "wide": wide,
            "gaps": gaps, "stuck": stuck, "report": report, "totals": totals}


def main() -> None:
    """CLI: прогнати M1 і коротко відзвітувати в консоль."""
    out = run_all(Path(__file__).resolve().parents[1])
    t = out["totals"]
    print(f"M1 готово: {t['total_hours']:,} годин, {t['sensors']} датчиків, "
          f"покриття {t['overall_coverage_pct']} % (кімнати {t['overall_coverage_rooms_pct']} %)")
    print(f"дірок > 1 год: {t['n_gaps_gt_1h']}, нечислових станів: {t['non_numeric_rows']}, "
          f"дублікатів: {t['duplicates']}, викидів: {t['out_of_range']}, "
          f"годин «застрягання»: {t['stuck_hours']}")
    for name, df in (("readings_hourly", out["hourly"]), ("readings_detailed", out["detailed"]),
                     ("weather_hourly", out["weather"]), ("hours_wide", out["wide"]),
                     ("gaps", out["gaps"])):
        print(f"  {name:20s} {df.shape}")


if __name__ == "__main__":
    main()
