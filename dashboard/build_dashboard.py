#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Збирає dashboard/index.html із dashboard/template.html + results/*.

Дані вшиваються в сторінку як JSON-блок `const DATA = {...}` — жодних
мережевих запитів під час перегляду. Запуск:

    PYTHONUTF8=1 python dashboard/build_dashboard.py
"""
from __future__ import annotations

import csv
import json
import pathlib
import datetime as dt

ROOT = pathlib.Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
HERE = ROOT / "dashboard"
TEMPLATE = HERE / "template.html"
OUTPUT = HERE / "index.html"

MARKER = "/*__DATA__*/null"

ROOM_UA = {"Bedroom": "кімната", "Kitchen": "кухня", "Printernya": "кладова"}
WEEKDAY_UA = {
    "Mon": "понеділок", "Tue": "вівторок", "Wed": "середа", "Thu": "четвер",
    "Fri": "п'ятниця", "Sat": "субота", "Sun": "неділя",
}
MONTH_SHORT_UA = {
    1: "січ", 2: "лют", 3: "бер", 4: "кві", 5: "тра", 6: "чер",
    7: "лип", 8: "сер", 9: "вер", 10: "жов", 11: "лис", 12: "гру",
}


# ---------------------------------------------------------------- helpers
def read_json(name: str) -> dict:
    with open(RESULTS / name, encoding="utf-8") as fh:
        return json.load(fh)


def read_csv(name: str) -> list[dict]:
    with open(RESULTS / name, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def num(value, digits: int | None = None):
    """CSV-рядок → float/int; порожньо → None."""
    if value is None:
        return None
    value = value.strip()
    if value == "" or value.lower() in {"nan", "none"}:
        return None
    try:
        out = float(value)
    except ValueError:
        return None
    if digits is not None:
        out = round(out, digits)
    return int(out) if digits == 0 else out


def parse_ts(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.strip())


def ms(value: str) -> int:
    return int(parse_ts(value).timestamp() * 1000)


def month_label(month: str) -> str:
    year, mon = month.split("-")
    return f"{MONTH_SHORT_UA[int(mon)]} {year[2:]}"


# ---------------------------------------------------------------- modules
def build_meta(m1: dict, m2: dict) -> dict:
    start = parse_ts(m1["ts_start_utc"])
    end = parse_ts(m1["ts_end_utc"])
    return {
        "period_start": start.strftime("%d.%m.%Y"),
        "period_end": end.strftime("%d.%m.%Y"),
        "built": dt.date.today().strftime("%d.%m.%Y"),
        "total_hours": m1["total_hours"],
        "sensors": m1["sensors"],
        "rows_hourly": m1["hourly_rows"],
        "rows_detailed": m1["detailed_rows"],
        "uptime_pct": round(m2["uptime_pct"], 1),
    }


def build_m2(m2: dict) -> dict:
    events = []
    for row in read_csv("m2_outages.csv"):
        start = parse_ts(row["start_kyiv"])
        events.append({
            "id": row["event_id"],
            "date": start.strftime("%d.%m.%Y"),
            "weekday": WEEKDAY_UA.get(row["weekday_kyiv"], row["weekday_kyiv"]),
            "time": start.strftime("%H:%M"),
            "hours": num(row["hours"], 1),
            "start_ms": ms(row["start_utc"]),
            "end_ms": ms(row["end_utc"]),
            "low_confidence": "НИЗЬКА ВПЕВНЕНІСТЬ" in (row["notes"] or ""),
        })

    monthly = []
    for key, value in sorted(m2.items()):
        if key.startswith("hours_20"):
            month = key.replace("hours_", "").replace("_", "-")
            monthly.append({"month": month, "label": month_label(month), "hours": value})

    period_start, period_end = ms(m2["period_start_utc"]), ms(m2["period_end_utc"])
    ticks, cursor = [], parse_ts(m2["period_start_utc"]).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0)
    end_dt = parse_ts(m2["period_end_utc"])
    while cursor <= end_dt:
        if cursor.timestamp() * 1000 >= period_start:
            ticks.append({"label": MONTH_SHORT_UA[cursor.month],
                          "ms": int(cursor.timestamp() * 1000)})
        cursor = (cursor.replace(day=28) + dt.timedelta(days=8)).replace(day=1)

    weekdays = [("sat", "субота"), ("sun", "неділя"), ("wed", "середа")]
    return {
        "events": events,
        "monthly": monthly,
        "timeline": {"start_ms": period_start, "end_ms": period_end, "ticks": ticks},
        "n_events": m2["n_events_total"],
        "total_hours": round(m2["total_hours"]),
        "median_hours": round(m2["median_hours"], 1),
        "max_hours": round(m2["max_hours"], 1),
        "max_date": parse_ts(m2["max_event_date"]).strftime("%d.%m.%Y"),
        "uptime_pct": round(m2["uptime_pct"], 1),
        "n_saturday": m2["events_weekday_sat"],
        "n_morning": sum(1 for e in events if 11 <= int(e["time"][:2]) <= 12),
        "weekday_hours": [{"key": k, "label": lab, "events": m2[f"events_weekday_{k}"],
                           "hours": m2[f"hours_weekday_{k}"]} for k, lab in weekdays],
    }


def build_m3(m3: dict) -> dict:
    cycle_rows = read_csv("m3_daily_cycle.csv")
    cycle = {
        "hours": [num(r["hour"], 0) for r in cycle_rows],
        "bedroom": [num(r["Bedroom"], 2) for r in cycle_rows],
        "kitchen": [num(r["Kitchen"], 2) for r in cycle_rows],
        "printernya": [num(r["Printernya"], 2) for r in cycle_rows],
        "outdoor": [num(r["Outdoor"], 2) for r in cycle_rows],
    }

    rooms = []
    for key in ("bedroom", "kitchen", "printernya"):
        rooms.append({
            "key": key,
            "label": ROOM_UA[key.capitalize() if key != "printernya" else "Printernya"],
            "mean": round(m3[f"{key}_mean_temp"], 1),
            "min": round(m3[f"{key}_min_temp"], 1),
            "max": round(m3[f"{key}_max_temp"], 1),
            "smoothing": round(m3[f"{key}_smoothing_x"], 1),
            "daily_range": round(m3[f"{key}_indoor_daily_range"], 1),
            "lag_h": m3[f"{key}_lag_h"],
            "lag_ok": bool(m3[f"{key}_lag_identifiable"]),
            "sens": round(m3[f"{key}_sensitivity"], 2),
            "sens_lo": round(m3[f"{key}_sensitivity_ci_low"], 2),
            "sens_hi": round(m3[f"{key}_sensitivity_ci_high"], 2),
            "r2": round(m3[f"{key}_r2"], 2),
            "n_hours": m3[f"{key}_n_hours"],
        })

    comfort = []
    for row in read_csv("m3_comfort.csv"):
        if row["month"] != "усе":
            continue
        comfort.append({
            "label": ROOM_UA[row["room"]],
            "key": row["room"].lower(),
            "cold": num(row["too_cold_pct"], 1),
            "ok": num(row["temp_ok_pct"], 1),
            "hot": num(row["too_hot_pct"], 1),
            "hours": num(row["n_hours_temp"], 0),
        })

    return {
        "cycle": cycle,
        "rooms": rooms,
        "comfort": comfort,
        "out_mean": round(m3["out_mean_temp"], 1),
        "out_min": round(m3["out_min_temp"], 1),
        "out_max": round(m3["out_max_temp"], 1),
        "out_range": round(m3["outdoor_daily_range"], 1),
        "peak_hour_outdoor": m3["peak_hour_outdoor"],
        "bedroom_peak_hour": m3["bedroom_peak_hour"],
        "comfort_low": round(m3["comfort_temp_low_c"]),
        "comfort_high": round(m3["comfort_temp_high_c"]),
    }


def build_m4(m4: dict) -> dict:
    grid = []
    for row in read_csv("m4_hours_table.csv"):
        grid.append({
            "t_out": num(row["t_out_c"], 0),
            "bedroom": num(row["hours_bedroom"], 0),
            "kitchen": num(row["hours_kitchen"], 0),
            "printernya": num(row["hours_printernya"], 0),
            "rate_bedroom": num(row["rate_c_per_h_bedroom"], 3),
        })
    return {
        "grid": grid,
        "t0": round(m4["t0_default_c"]),
        "target": round(m4["threshold_c"]),
        "hours_minus10": round(m4["hours_to_18_from_22_at_minus10"]),
        "rate_minus10": round(m4["cooling_rate_c_per_h_at_minus10"], 2),
        "k_bedroom": round(m4["k_bedroom"], 4),
        "k_bedroom_lo": round(m4["k_bedroom_ci_low"], 4),
        "k_bedroom_hi": round(m4["k_bedroom_ci_high"], 4),
        "n_events": m4["n_events_used"],
        "note": m4["limitation_note"],
    }


def build_m5(m5: dict) -> dict:
    monthly = []
    for row in read_csv("m5_monthly_gap.csv"):
        gap_b, gap_k = num(row["gap_bedroom_c"], 2), num(row["gap_kitchen_c"], 2)
        if gap_b is None and gap_k is None:
            continue
        monthly.append({
            "month": row["month"],
            "label": month_label(row["month"]),
            "bedroom": gap_b,
            "kitchen": gap_k,
        })
    return {
        "monthly": monthly,
        "gap_bedroom": round(m5["printernya_minus_bedroom_c"], 1),
        "gap_kitchen": round(m5["printernya_minus_kitchen_c"], 1),
        "nvme_coef": round(m5["coef_nvme_per_1c"], 2),
        "nvme_r2": round(m5["r2_nvme_model"], 2),
        "pc_coef": round(m5["coef_pc_per_100w"], 3),
        "pc_p": round(m5["p_pc"], 2),
        "pc_on_pct": round(m5["pc_on_share_pct"], 1),
        "active_delta": round(m5["active_vs_idle_delta_c"], 1),
        "active_p": round(m5["p_active_vs_idle"], 2),
        "n_hours": m5["n_tech_hours"],
    }


def build_m6(m6: dict) -> dict:
    counts = [0] * 24
    counts_detailed = [0] * 24
    n_hourly = 0
    for row in read_csv("m6_cooking_events.csv"):
        hour = num(row["hour_kyiv"], 0)
        if hour is None:
            continue
        if row["source"] == "hourly":
            counts[hour] += 1
            n_hourly += 1
        else:
            counts_detailed[hour] += 1
    return {
        "hours": list(range(24)),
        "counts": counts,
        "counts_detailed": counts_detailed,
        "n_hourly": n_hourly,
        "n_detailed": m6["n_events_detailed"],
        "rise_pp": round(m6["mean_rise_pp"], 1),
        "return_min": round(m6["mean_return_min"]),
        "duration_min": round(m6["mean_duration_min"]),
        "peak_hour": m6["peak_hour_kyiv"],
        "evening_pct": round(m6["share_evening_pct"], 1),
        "per_week": round(m6["events_per_week_hourly"], 1),
        "coverage_pct": round(m6["kitchen_coverage_pct"], 1),
    }


def build_m7(m7: dict) -> dict:
    models = [
        ("persistence", "«як годину тому»", "базовий", m7["mae_persistence"], m7["rmse_persistence"]),
        ("seasonal", "«як учора о цій порі»", "базовий", m7["mae_seasonal_naive"], m7["rmse_seasonal_naive"]),
        ("climatology", "середнє по годині доби", "базовий", m7["mae_climatology"], m7["rmse_climatology"]),
        ("ridge", "лінійна (Ridge)", "модель", m7["mae_ridge"], m7["rmse_ridge"]),
        ("hgb", "бустинг (HGB)", "модель", m7["mae_hgb"], m7["rmse_hgb"]),
    ]
    return {
        "models": [{"key": k, "label": lab, "kind": kind,
                    "mae": round(mae, 3), "rmse": round(rmse, 3)} for k, lab, kind, mae, rmse in models],
        "best": m7["best_model"],
        "best_mae": round(m7["best_mae"], 2),
        "gain": round(m7["gain_vs_best_baseline_c"], 2),
        "gain_pct": round(100 * m7["gain_vs_best_baseline_c"] / m7["mae_persistence"]),
        "horizon_h": m7["horizon_h"],
        "n_train": m7["n_train"],
        "n_test": m7["n_test"],
        "test_start": parse_ts(m7["test_start"]).strftime("%d.%m.%Y"),
        "features": [f.strip() for f in m7["top_features"].split(",")][:4],
    }


def build_m1(m1: dict) -> dict:
    group_of = {"Bedroom": "bedroom", "Kitchen": "kitchen",
                "Printernya": "printernya", "Soil": "other", "Tech": "other"}
    sensors = []
    for row in read_csv("m1_quality.csv"):
        sensors.append({
            "label": row["label_ua"],
            "group": group_of.get(row["room"], "other"),
            "coverage": num(row["coverage_pct"], 1),
            "hours": num(row["present_hours"], 0),
            "calendar": num(row["calendar_hours"], 0),
        })
    sensors.sort(key=lambda s: s["coverage"], reverse=True)
    return {
        "sensors": sensors,
        "coverage_all": round(m1["overall_coverage_pct"], 1),
        "coverage_rooms": round(m1["overall_coverage_rooms_pct"], 1),
        "duplicates": m1["duplicates"],
        "out_of_range": m1["out_of_range"],
        "stuck_hours": m1["stuck_hours"],
        "longest_stuck": m1["longest_stuck_hours"],
        "n_stuck_runs": m1["n_stuck_runs"],
        "non_numeric": m1["non_numeric_rows"],
        "n_gaps": m1["n_gaps_gt_1h"],
        "longest_gap": m1["longest_gap_hours"],
    }


# ---------------------------------------------------------------- main
def main() -> None:
    m1, m2 = read_json("m1_quality.json"), read_json("m2_outages.json")
    m3, m4 = read_json("m3_climate.json"), read_json("m4_cooling.json")
    m5, m6 = read_json("m5_tech.json"), read_json("m6_cooking.json")
    m7 = read_json("m7_model.json")

    data = {
        "meta": build_meta(m1, m2),
        "m1": build_m1(m1),
        "m2": build_m2(m2),
        "m3": build_m3(m3),
        "m4": build_m4(m4),
        "m5": build_m5(m5),
        "m6": build_m6(m6),
        "m7": build_m7(m7),
    }

    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    template = TEMPLATE.read_text(encoding="utf-8")
    if MARKER not in template:
        raise SystemExit(f"У шаблоні немає маркера {MARKER!r}")
    OUTPUT.write_text(template.replace(MARKER, payload), encoding="utf-8")

    size_kb = OUTPUT.stat().st_size / 1024
    print(f"OK  {OUTPUT.relative_to(ROOT)}  {size_kb:.1f} KB "
          f"(дані {len(payload) / 1024:.1f} KB, {len(data) - 1} модулів)")


if __name__ == "__main__":
    main()
