"""Модуль M5 — скільки градусів додає техніка (проєкт «4331 година»).

Питання: на скільки 3D-принтер, NAS і сервер підвищують температуру в кладовій
(Printernya) — і чи «зайвий» градус у ПК-кімнаті (Bedroom) справді від ПК,
а не від вулиці?

Метод (специфікація, розділ M5):
  1. різниця кладова - житлова / кладова - кухня, в середньому, по місяцях,
     по годині доби (Київ);
  2. кореляції printernya_temp з дисками NAS, NVMe, завантаженням сервера,
     вуличною температурою;
  3. лінійна регресія printernya_temp ~ out_temp + nas_mean_temp
     (+ версії з лагом NAS 1-3 год) — контроль за вулицею, щоб не переплутати
     причину; порівняння «техніка активна / спить»;
  4. той самий контроль для ПК в житловій кімнаті: bedroom_temp ~ out_temp + pc_power.

`pc_power` = NaN означає «ПК вимкнений» (факт, 34 % покриття, не дірка) —
переводимо в 0 Вт і додаємо прапорець `pc_on`. NAS/сервер NaN — це справжні
дірки (пристрій ще не стояв або відвалився) — такі години прибираємо з
кореляцій/регресій.

Запуск: `PYTHONUTF8=1 python -m src.tech_heat`
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.formula.api as smf
from scipy import stats

KYIV = "Europe/Kyiv"
NAS_COLS: list[str] = ["nas_sda_temp", "nas_sdb_temp", "nas_nvme_temp"]
LAG_HOURS: list[int] = [1, 2, 3]


# ── 1. Завантаження і підготовка ────────────────────────────────────────────────
def load_wide(root: str | Path) -> pd.DataFrame:
    """Читає `data/clean/hours_wide.csv`, парсить `ts`/`ts_kyiv`, сортує за часом."""
    df = pd.read_csv(Path(root) / "data" / "clean" / "hours_wide.csv")
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df["ts_kyiv"] = pd.to_datetime(df["ts_kyiv"], utc=True).dt.tz_convert(KYIV)
    return df.sort_values("ts").reset_index(drop=True)


def add_tech_features(df: pd.DataFrame) -> pd.DataFrame:
    """Додає похідні колонки для M5, не чіпаючи hours_wide.csv.

    * `pc_on` — прапорець «ПК працював»: `pc_power` не NaN, до заповнення.
    * `pc_power` — NaN -> 0 Вт (ПК вимкнений — це факт, а не дірка).
    * `nas_mean_temp` — середнє трьох датчиків NAS (sda/sdb/nvme); NaN, якщо
      бракує хоч одного (на практиці всі три зникають одночасно — див. нижче).
    * `nas_mean_temp_lag{1,2,3}h` — той самий ряд, зсунутий на 1-3 години назад
      (таблиця — суцільний погодинний календар, тож зсув рядків = зсув у часі).
    """
    out = df.copy()
    out["pc_on"] = out["pc_power"].notna()
    out["pc_power"] = out["pc_power"].fillna(0.0)
    out["nas_mean_temp"] = out[NAS_COLS].mean(axis=1, skipna=False)
    for lag in LAG_HOURS:
        out[f"nas_mean_temp_lag{lag}h"] = out["nas_mean_temp"].shift(lag)
    return out


def tech_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Підмножина годин, придатна для кореляцій/регресій техніки в кладовій.

    Прибираємо години, де немає printernya_temp, out_temp, nas_mean_temp або
    server_cpu_load — це справжні дірки (NAS ще не стояв або відвалився),
    а не «ПК вимкнений».
    """
    cols = ["printernya_temp", "out_temp", "nas_mean_temp", "server_cpu_load"]
    return df.dropna(subset=cols).reset_index(drop=True)


# ── 2. Наскільки кладова тепліша ────────────────────────────────────────────────
def room_gap_overall(df: pd.DataFrame) -> dict:
    """Середня різниця кладова - житлова і кладова - кухня, °C (парне видалення NaN)."""
    gap_bed = (df["printernya_temp"] - df["bedroom_temp"]).mean()
    gap_kit = (df["printernya_temp"] - df["kitchen_temp"]).mean()
    return {"printernya_minus_bedroom_c": round(float(gap_bed), 2),
            "printernya_minus_kitchen_c": round(float(gap_kit), 2)}


def room_gap_monthly(df: pd.DataFrame) -> pd.DataFrame:
    """Середні температури й різниці по місяцях (київський календар)."""
    g = df.copy()
    g["month"] = g["ts_kyiv"].dt.tz_localize(None).dt.to_period("M").astype(str)
    agg = g.groupby("month").agg(
        printernya_temp_mean=("printernya_temp", "mean"),
        bedroom_temp_mean=("bedroom_temp", "mean"),
        kitchen_temp_mean=("kitchen_temp", "mean"),
        out_temp_mean=("out_temp", "mean"),
        n_hours=("ts", "size"),
    )
    agg["gap_bedroom_c"] = agg["printernya_temp_mean"] - agg["bedroom_temp_mean"]
    agg["gap_kitchen_c"] = agg["printernya_temp_mean"] - agg["kitchen_temp_mean"]
    return agg.round(2).reset_index()


def room_gap_by_hour(df: pd.DataFrame) -> pd.DataFrame:
    """Середні різниці по годині доби (Київ) — денний цикл теплового запасу кладової."""
    g = df.copy()
    g["hour"] = g["ts_kyiv"].dt.hour
    agg = g.groupby("hour").agg(
        printernya_temp_mean=("printernya_temp", "mean"),
        gap_bedroom_c=("printernya_temp", lambda s: (s - g.loc[s.index, "bedroom_temp"]).mean()),
        gap_kitchen_c=("printernya_temp", lambda s: (s - g.loc[s.index, "kitchen_temp"]).mean()),
        server_cpu_load_mean=("server_cpu_load", "mean"),
    )
    return agg.round(2).reset_index()


# ── 3. Кореляції ────────────────────────────────────────────────────────────────
def correlations(tech: pd.DataFrame) -> dict:
    """Кореляції printernya_temp з дисками NAS, NVMe, сервером і вулицею."""
    cols = ["nas_sda_temp", "nas_sdb_temp", "nas_nvme_temp", "nas_mean_temp",
            "server_cpu_load", "out_temp"]
    corr = {c: round(float(tech["printernya_temp"].corr(tech[c])), 3) for c in cols}
    return corr


# ── 4. Регресія з контролем за вулицею ──────────────────────────────────────────
def fit_nas_regression(tech: pd.DataFrame, nas_col: str = "nas_mean_temp") -> dict:
    """OLS `printernya_temp ~ out_temp + nas_col`. Повертає коефіцієнт NAS, CI, R², p-value."""
    sub = tech.dropna(subset=["printernya_temp", "out_temp", nas_col])
    model = smf.ols(f"printernya_temp ~ out_temp + {nas_col}", data=sub).fit()
    ci = model.conf_int().loc[nas_col]
    return {
        "n": int(model.nobs),
        "coef_nas_per_1c": round(float(model.params[nas_col]), 3),
        "coef_nas_ci_low": round(float(ci[0]), 3),
        "coef_nas_ci_high": round(float(ci[1]), 3),
        "p_nas": float(model.pvalues[nas_col]),
        "coef_out_temp": round(float(model.params["out_temp"]), 3),
        "r2": round(float(model.rsquared), 3),
        "model": model,
    }


def compare_nas_lags(tech: pd.DataFrame) -> pd.DataFrame:
    """Той самий регресія з NAS-температурою без лагу і з лагом 1-3 год — де відгук найсильніший."""
    rows = []
    for lag in [0, *LAG_HOURS]:
        col = "nas_mean_temp" if lag == 0 else f"nas_mean_temp_lag{lag}h"
        res = fit_nas_regression(tech, col)
        rows.append({"lag_hours": lag, "n": res["n"], "coef_nas_per_1c": res["coef_nas_per_1c"],
                     "p_nas": round(res["p_nas"], 4), "r2": res["r2"]})
    return pd.DataFrame(rows)


def active_vs_idle(tech: pd.DataFrame) -> dict:
    """«Техніка активна / спить»: NAS mean temp вище/нижче медіани, з контролем на вулицю.

    Контроль: беремо залишки регресії `printernya_temp ~ out_temp` (тобто
    температуру кладової «мінус те, що пояснює вулиця») і порівнюємо середній
    залишок у двох станах — так різниця не переплутається з погодою.
    """
    base = smf.ols("printernya_temp ~ out_temp", data=tech).fit()
    resid = base.resid
    median_nas = tech["nas_mean_temp"].median()
    active = tech["nas_mean_temp"] > median_nas
    r_active, r_idle = resid[active], resid[~active]
    tstat, pval = stats.ttest_ind(r_active, r_idle, equal_var=False)
    # додатковий погляд — той самий поділ, але за завантаженням сервера
    median_load = tech["server_cpu_load"].median()
    active_load = tech["server_cpu_load"] > median_load
    delta_load = resid[active_load].mean() - resid[~active_load].mean()
    # той самий поділ, але за NVMe (менш «шумний» проксі — див. кореляції)
    median_nvme = tech["nas_nvme_temp"].median()
    active_nvme = tech["nas_nvme_temp"] > median_nvme
    delta_nvme = resid[active_nvme].mean() - resid[~active_nvme].mean()
    _, pval_nvme = stats.ttest_ind(resid[active_nvme], resid[~active_nvme], equal_var=False)
    return {
        "median_nas_mean_temp_c": round(float(median_nas), 2),
        "active_vs_idle_delta_c": round(float(r_active.mean() - r_idle.mean()), 2),
        "p_active_vs_idle": float(pval),
        "n_active": int(active.sum()), "n_idle": int((~active).sum()),
        "delta_c_by_server_load_median": round(float(delta_load), 2),
        "delta_c_by_nvme_median": round(float(delta_nvme), 2),
        "p_active_vs_idle_nvme": float(pval_nvme),
    }


# ── 5. ПК у житловій кімнаті ─────────────────────────────────────────────────────
def pc_effect(df: pd.DataFrame) -> dict:
    """bedroom_temp ~ out_temp + pc_power, і порівняння «ПК увімкнений / вимкнений» з контролем на вулицю."""
    sub = df.dropna(subset=["bedroom_temp", "out_temp"]).copy()
    model = smf.ols("bedroom_temp ~ out_temp + pc_power", data=sub).fit()
    ci = model.conf_int().loc["pc_power"] * 100

    base = smf.ols("bedroom_temp ~ out_temp", data=sub).fit()
    resid = base.resid
    on, off = sub["pc_on"], ~sub["pc_on"]
    tstat, pval = stats.ttest_ind(resid[on], resid[off], equal_var=False)

    return {
        "n": int(model.nobs),
        "pc_on_share_pct": round(float(df["pc_on"].mean() * 100), 1),
        "coef_pc_per_100w": round(float(model.params["pc_power"] * 100), 3),
        "coef_pc_per_100w_ci_low": round(float(ci[0]), 3),
        "coef_pc_per_100w_ci_high": round(float(ci[1]), 3),
        "p_pc": float(model.pvalues["pc_power"]),
        "pc_delta_c": round(float(resid[on].mean() - resid[off].mean()), 2),
        "p_pc_on_vs_off": float(pval),
        "r2": round(float(model.rsquared), 3),
        "model": model,
    }


# ── 6. Усе разом ─────────────────────────────────────────────────────────────────
def run_all(root: str | Path = ".") -> dict:
    """Проганяє весь M5 і записує `results/m5_tech.json`, `results/m5_monthly_gap.csv`."""
    root = Path(root)
    res_dir = root / "results"
    res_dir.mkdir(parents=True, exist_ok=True)

    wide = load_wide(root)
    df = add_tech_features(wide)
    tech = tech_frame(df)

    gap_overall = room_gap_overall(df)
    monthly = room_gap_monthly(df)
    hourly = room_gap_by_hour(df)
    corr = correlations(tech)
    nas_reg = fit_nas_regression(tech)
    nvme_reg = fit_nas_regression(tech, "nas_nvme_temp")
    lag_table = compare_nas_lags(tech)
    aci = active_vs_idle(tech)
    pc = pc_effect(df)

    summary = {
        **gap_overall,
        "corr_nas": corr["nas_mean_temp"],
        "corr_nas_sda": corr["nas_sda_temp"],
        "corr_nas_sdb": corr["nas_sdb_temp"],
        "corr_nas_nvme": corr["nas_nvme_temp"],
        "corr_server_load": corr["server_cpu_load"],
        "corr_out_temp": corr["out_temp"],
        "coef_nas_per_1c": nas_reg["coef_nas_per_1c"],
        "coef_nas_ci_low": nas_reg["coef_nas_ci_low"],
        "coef_nas_ci_high": nas_reg["coef_nas_ci_high"],
        "p_nas": round(nas_reg["p_nas"], 5),
        "coef_out_temp_printernya": nas_reg["coef_out_temp"],
        "r2": nas_reg["r2"],
        "n_tech_hours": tech_frame(df).shape[0],
        "best_nas_lag_hours": int(lag_table.loc[lag_table["r2"].idxmax(), "lag_hours"]),
        # диски sda/sdb — «шумний» проксі (стрибки від дискової активності, не від тепла);
        # NVMe тримає вузький діапазон і явно відгукується на кімнатну/вуличну температуру.
        "coef_nvme_per_1c": nvme_reg["coef_nas_per_1c"],
        "p_nvme": round(nvme_reg["p_nas"], 5),
        "r2_nvme_model": nvme_reg["r2"],
        "active_vs_idle_delta_c": aci["active_vs_idle_delta_c"],
        "p_active_vs_idle": round(aci["p_active_vs_idle"], 5),
        "median_nas_mean_temp_c": aci["median_nas_mean_temp_c"],
        "pc_on_share_pct": pc["pc_on_share_pct"],
        "pc_delta_c": pc["pc_delta_c"],
        "p_pc_on_vs_off": round(pc["p_pc_on_vs_off"], 5),
        "coef_pc_per_100w": pc["coef_pc_per_100w"],
        "coef_pc_per_100w_ci_low": pc["coef_pc_per_100w_ci_low"],
        "coef_pc_per_100w_ci_high": pc["coef_pc_per_100w_ci_high"],
        "p_pc": round(pc["p_pc"], 5),
        "r2_pc": pc["r2"],
    }

    monthly.to_csv(res_dir / "m5_monthly_gap.csv", index=False)
    with open(res_dir / "m5_tech.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    return {"wide": wide, "df": df, "tech": tech, "monthly": monthly, "hourly": hourly,
            "corr": corr, "nas_reg": nas_reg, "lag_table": lag_table, "active_vs_idle": aci,
            "pc": pc, "summary": summary}


if __name__ == "__main__":
    import pathlib
    out = run_all(pathlib.Path(__file__).resolve().parents[1])
    print(json.dumps(out["summary"], ensure_ascii=False, indent=2))
