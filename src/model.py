"""Модуль M7 — модель-прогноз, експеримент (проєкт «4331 година»).

Питання: чи можна передбачити температуру в квартирі на 24 години вперед,
знаючи вулицю й час?

Ціль (`target`): температура кімнати в момент `t + 24 год`, за ознаками,
відомими в момент `t`. Основна ціль — `bedroom_temp` (житлова кімната,
єдина з вікном, найважливіша для комфорту). `printernya_temp` — другий,
короткий прогін для порівняння.

**Головне припущення експерименту**: серед ознак є вулична температура
(і вологість/вітер/хмарність) **у момент t+24**, тобто саме тоді, на яку
годину робимо прогноз. У реальному житті на момент `t` ми такого значення
не знаємо — знаємо лише прогноз погоди. Тут замість прогнозу підставляємо
**фактичну** погоду з t+24, тобто вдаємо, що маємо ідеальний прогноз погоди
на добу вперед. Це свідомо оптимістичний сценарій: він показує, наскільки
взагалі корисна інформація «яка погода буде надворі» для прогнозу
температури в квартирі, а не якість реального прогнозу погоди.

«Застряглий» датчик житлової кімнати (18–24.08, 144 год, + короткий епізод
22–23.03) виключаємо: `stuck=True` → значення в NaN ще до побудови ознак,
тому воно не потрапляє ні в ціль, ні в лаги, ні в ковзне середнє.

Запуск: `PYTHONUTF8=1 python -m src.model`
Пише: `results/m7_model.json`, `results/m7_predictions.csv`.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.inspection import permutation_importance
from sklearn.linear_model import RidgeCV
from sklearn.metrics import mean_absolute_error, mean_squared_error
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from .cleaning import ROOM_COLORS, ROOM_LABELS_UA

KYIV = "Europe/Kyiv"

# ── Гіперпараметри експерименту ─────────────────────────────────────────────
HORIZON_H = 24                       # на скільки годин вперед прогнозуємо
LAGS_H: tuple[int, ...] = (1, 3, 6, 12, 24)
ROLL_WINDOW_H = 24
TRAIN_FRAC = 0.8                     # перші 80 % за часом — train, останні 20 % — test
RANDOM_STATE = 42

#: цільова колонка hours_wide -> entity_id у readings_hourly (для прапорця stuck)
ENTITY_FOR_TARGET: dict[str, str] = {
    "bedroom_temp": "sensor.test_product_temperature",
    "kitchen_temp": "sensor.test_product_temperature_2",
    "printernya_temp": "sensor.test_product_temperature_3",
}

#: колонка hours_wide -> кімната (для кольору/підпису з cleaning.py)
TARGET_ROOM: dict[str, str] = {
    "bedroom_temp": "Bedroom",
    "kitchen_temp": "Kitchen",
    "printernya_temp": "Printernya",
}

#: порядок ознак — контракт між build_supervised і моделями
FEATURE_COLS: list[str] = [
    "room_temp_t", "lag_1h", "lag_3h", "lag_6h", "lag_12h", "lag_24h", "roll24_mean",
    "out_temp_fcst_t24", "out_hum_fcst_t24", "out_wind_fcst_t24", "out_cloud_fcst_t24",
    "hour", "dow", "month",
]

#: українські підписи ознак — для графіка важливості
FEATURE_LABELS_UA: dict[str, str] = {
    "room_temp_t": "температура зараз",
    "lag_1h": "температура 1 год тому",
    "lag_3h": "температура 3 год тому",
    "lag_6h": "температура 6 год тому",
    "lag_12h": "температура 12 год тому",
    "lag_24h": "температура 24 год тому (учора)",
    "roll24_mean": "середнє за останні 24 год",
    "out_temp_fcst_t24": "вулична температура (+24 год)",
    "out_hum_fcst_t24": "вулична вологість (+24 год)",
    "out_wind_fcst_t24": "вітер (+24 год)",
    "out_cloud_fcst_t24": "хмарність (+24 год)",
    "hour": "година доби",
    "dow": "день тижня",
    "month": "місяць",
}

#: українські підписи методів — для таблиць і графіків
METHOD_LABELS_UA: dict[str, str] = {
    "persistence": "база: як зараз",
    "seasonal_naive": "база: як 24 год тому",
    "climatology": "база: середнє за годину доби",
    "ridge": "Ridge (лінійна регресія)",
    "hgb": "Gradient Boosting",
}
BASELINE_METHODS = ("persistence", "seasonal_naive", "climatology")
MODEL_METHODS = ("ridge", "hgb")


# ── Дані ─────────────────────────────────────────────────────────────────────
def load_clean(root: str | Path) -> pd.DataFrame:
    """`data/clean/hours_wide.csv` з правильними типами ts, відсортована за часом."""
    root = Path(root)
    df = pd.read_csv(root / "data" / "clean" / "hours_wide.csv")
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.sort_values("ts").set_index("ts")


def stuck_mask(root: str | Path, entity_id: str) -> pd.Series:
    """Прапорець `stuck` для одного датчика, індексований по ts (UTC)."""
    root = Path(root)
    rh = pd.read_csv(root / "data" / "clean" / "readings_hourly.csv")
    rh["ts"] = pd.to_datetime(rh["ts"], utc=True)
    sub = rh.loc[rh["entity_id"] == entity_id, ["ts", "stuck"]].drop_duplicates("ts")
    return sub.set_index("ts")["stuck"].astype(bool)


# ── Ознаки ───────────────────────────────────────────────────────────────────
def build_supervised(
    wide: pd.DataFrame,
    target_col: str,
    root: str | Path,
    horizon: int = HORIZON_H,
    lags: tuple[int, ...] = LAGS_H,
    roll_window: int = ROLL_WINDOW_H,
    exclude_stuck: bool = True,
) -> pd.DataFrame:
    """Будує таблицю ознака->значення + `y` (ціль у t+horizon), індекс — ts (t), UTC.

    Ознаки — усе, що відомо в момент t:
      * `room_temp_t`, лаги 1/3/6/12/24 год, ковзне середнє за 24 год — своя ж кімната;
      * `out_temp_fcst_t24` та вологість/вітер/хмарність +24 год — **фактична** погода
        на момент цілі (припущення «ідеальний прогноз», див. докстрінг модуля);
      * `hour`, `dow`, `month` — календарні ознаки моменту t. Через те що горизонт
        рівно 24 год, година доби в t збігається з годиною доби цілі (крім двох діб
        переходу на літній/зимовий час) — тому цю саму колонку можна читати і як
        «година доби, на яку прогнозуємо».

    «Застряглі» години датчика (`stuck=True`) перетворюємо на NaN ще до розрахунку
    ознак — тому вони не потрапляють ні в лаги, ні в ковзне середнє, ні в ціль.
    Рядки з будь-яким NaN (краї ряду, дірки, застрягання) відкидаємо.
    """
    room = wide[target_col].copy()

    if exclude_stuck and target_col in ENTITY_FOR_TARGET:
        stuck = (stuck_mask(root, ENTITY_FOR_TARGET[target_col])
                 .reindex(wide.index).fillna(False).astype(bool))
        room = room.mask(stuck)

    feat = pd.DataFrame(index=wide.index)
    feat["room_temp_t"] = room
    for lag in lags:
        feat[f"lag_{lag}h"] = room.shift(lag)
    feat["roll24_mean"] = room.rolling(roll_window, min_periods=roll_window).mean()

    feat["out_temp_fcst_t24"] = wide["out_temp"].shift(-horizon)
    feat["out_hum_fcst_t24"] = wide["out_hum"].shift(-horizon)
    feat["out_wind_fcst_t24"] = wide["out_wind"].shift(-horizon)
    feat["out_cloud_fcst_t24"] = wide["out_cloud"].shift(-horizon)

    kyiv_index = wide.index.tz_convert(KYIV)
    feat["hour"] = kyiv_index.hour
    feat["dow"] = kyiv_index.dayofweek
    feat["month"] = kyiv_index.month

    feat["y"] = room.shift(-horizon)

    return feat.dropna()


def time_split(supervised: pd.DataFrame, train_frac: float = TRAIN_FRAC) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Поділ за часом, без перемішування: перші `train_frac` рядків — train, решта — test.

    Чому не випадково: сусідні години сильно автокорельовані (і температура в
    кімнаті, і погода). Випадковий поділ дав би тесту рядки, чиї сусіди по часу
    вже в train — модель підглядала б «майбутнє» і MAE виглядав би кращим, ніж
    буде на справді нових даних. Поділ за часом імітує реальне використання:
    модель навчена на минулому, перевіряється на майбутньому, якого не бачила.
    """
    n_train = int(len(supervised) * train_frac)
    return supervised.iloc[:n_train], supervised.iloc[n_train:]


# ── Базові прогнози ──────────────────────────────────────────────────────────
def climatology_by_hour(train: pd.DataFrame) -> pd.Series:
    """Середнє `y` по годині доби (тільки з train) — «типова температура о Х год»."""
    return train.groupby("hour")["y"].mean()


def baseline_preds(train: pd.DataFrame, test: pd.DataFrame) -> dict[str, pd.Series]:
    """Три базові прогнози, вирівняні по індексу test:

    * `persistence`     — T(t): «за 24 год температура не зміниться»;
    * `seasonal_naive`  — T(t-24), тобто лаг 24 год: «як 24 год тому відносно
      *зараз*» (не плутати з persistence: тут беремо точку ще на добу раніше,
      бо горизонт прогнозу сам дорівнює одній добі — інакше два базові прогнози
      збіглися б і порівнювати не було б з чим);
    * `climatology`     — середнє за train для тієї самої години доби.
    """
    clim = climatology_by_hour(train)
    return {
        "persistence": test["room_temp_t"],
        "seasonal_naive": test["lag_24h"],
        "climatology": test["hour"].map(clim),
    }


# ── Моделі ───────────────────────────────────────────────────────────────────
def fit_ridge(X_train: pd.DataFrame, y_train: pd.Series) -> Pipeline:
    """Лінійна регресія з L2-регуляризацією (Ridge), alpha підбирається сам (RidgeCV)."""
    pipe = Pipeline([
        ("scaler", StandardScaler()),
        ("ridge", RidgeCV(alphas=np.logspace(-3, 3, 13))),
    ])
    pipe.fit(X_train, y_train)
    return pipe


def fit_hgb(X_train: pd.DataFrame, y_train: pd.Series) -> HistGradientBoostingRegressor:
    model = HistGradientBoostingRegressor(
        random_state=RANDOM_STATE, max_iter=300, learning_rate=0.05, max_depth=6,
    )
    model.fit(X_train, y_train)
    return model


# ── Оцінка ───────────────────────────────────────────────────────────────────
def evaluate(y_true: pd.Series, preds: dict[str, pd.Series]) -> pd.DataFrame:
    """Таблиця method / mae / rmse, відсортована за MAE (найкраще — зверху)."""
    rows = []
    for name, p in preds.items():
        p = p.reindex(y_true.index)
        mae = mean_absolute_error(y_true, p)
        rmse = float(np.sqrt(mean_squared_error(y_true, p)))
        rows.append({"method": name, "mae": float(mae), "rmse": rmse})
    return pd.DataFrame(rows).sort_values("mae").reset_index(drop=True)


def top_permutation_importance(
    model, X_test: pd.DataFrame, y_test: pd.Series, feature_cols: list[str], top_n: int = 8,
) -> pd.Series:
    """Permutation importance (падіння якості = зростання MAE) для тестової вибірки."""
    result = permutation_importance(
        model, X_test, y_test, n_repeats=10, random_state=RANDOM_STATE,
        scoring="neg_mean_absolute_error",
    )
    imp = pd.Series(result.importances_mean, index=feature_cols).sort_values(ascending=False)
    return imp.head(top_n)


# ── Оркестрація одного таргету ───────────────────────────────────────────────
def run_target(root: str | Path, target_col: str, train_frac: float = TRAIN_FRAC) -> dict:
    """Повний прогін для однієї цілі: ознаки -> поділ -> базові -> моделі -> метрики."""
    root = Path(root)
    wide = load_clean(root)
    supervised = build_supervised(wide, target_col, root)
    train, test = time_split(supervised, train_frac)

    X_train, y_train = train[FEATURE_COLS], train["y"]
    X_test, y_test = test[FEATURE_COLS], test["y"]

    preds = baseline_preds(train, test)
    ridge = fit_ridge(X_train, y_train)
    hgb = fit_hgb(X_train, y_train)
    preds["ridge"] = pd.Series(ridge.predict(X_test), index=test.index)
    preds["hgb"] = pd.Series(hgb.predict(X_test), index=test.index)

    metrics = evaluate(y_test, preds)
    importances = top_permutation_importance(hgb, X_test, y_test, FEATURE_COLS, top_n=8)

    return {
        "target": target_col,
        "wide": wide, "supervised": supervised, "train": train, "test": test,
        "y_test": y_test, "preds": preds, "metrics": metrics,
        "ridge": ridge, "hgb": hgb, "importances": importances,
        "climatology": climatology_by_hour(train),
    }


# ── Повний M7: обидві цілі + запис результатів ───────────────────────────────
def run_m7(root: str | Path = ".") -> dict:
    """Прогонає M7 (bedroom_temp основний, printernya_temp — короткий другий прогін)

    і пише `results/m7_model.json`, `results/m7_predictions.csv`.
    """
    root = Path(root)
    res_dir = root / "results"
    res_dir.mkdir(parents=True, exist_ok=True)

    bedroom = run_target(root, "bedroom_temp")
    printernya = run_target(root, "printernya_temp")

    m = bedroom["metrics"].set_index("method")
    best_model = m.loc[list(MODEL_METHODS), "mae"].astype(float).idxmin()
    best_mae = float(m.loc[best_model, "mae"])
    best_baseline = m.loc[list(BASELINE_METHODS), "mae"].astype(float).idxmin()
    best_baseline_mae = float(m.loc[best_baseline, "mae"])
    gain = round(best_baseline_mae - best_mae, 4)

    pm = printernya["metrics"].set_index("method")

    def r(x: float) -> float:
        return round(float(x), 4)

    result = {
        "target": "bedroom_temp",
        "horizon_h": HORIZON_H,
        "n_train": int(len(bedroom["train"])),
        "n_test": int(len(bedroom["test"])),
        "test_start": str(bedroom["test"].index.min()),
        "mae_persistence": r(m.loc["persistence", "mae"]),
        "mae_seasonal_naive": r(m.loc["seasonal_naive", "mae"]),
        "mae_climatology": r(m.loc["climatology", "mae"]),
        "mae_ridge": r(m.loc["ridge", "mae"]),
        "mae_hgb": r(m.loc["hgb", "mae"]),
        "rmse_persistence": r(m.loc["persistence", "rmse"]),
        "rmse_seasonal_naive": r(m.loc["seasonal_naive", "rmse"]),
        "rmse_climatology": r(m.loc["climatology", "rmse"]),
        "rmse_ridge": r(m.loc["ridge", "rmse"]),
        "rmse_hgb": r(m.loc["hgb", "rmse"]),
        "best_model": best_model,
        "best_mae": r(best_mae),
        "gain_vs_best_baseline_c": gain,
        "top_features": ",".join(bedroom["importances"].head(8).index.tolist()),
        "printernya_mae_hgb": r(pm.loc["hgb", "mae"]),
        "printernya_mae_persistence": r(pm.loc["persistence", "mae"]),
    }

    (res_dir / "m7_model.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    pred_df = pd.DataFrame({
        "ts": bedroom["test"].index,
        "actual": bedroom["y_test"].to_numpy(),
        "pred_best": bedroom["preds"][best_model].reindex(bedroom["test"].index).to_numpy(),
        "pred_baseline": bedroom["preds"][best_baseline].reindex(bedroom["test"].index).to_numpy(),
    })
    pred_df.to_csv(res_dir / "m7_predictions.csv", index=False)

    return {
        "bedroom": bedroom, "printernya": printernya, "result": result,
        "best_model": best_model, "best_baseline": best_baseline, "pred_df": pred_df,
    }


def main() -> None:
    """CLI: прогнати M7 і коротко відзвітувати в консоль."""
    out = run_m7(Path(__file__).resolve().parents[1])
    r = out["result"]
    print(f"M7 готово: ціль {r['target']}, горизонт {r['horizon_h']} год, "
          f"train {r['n_train']} / test {r['n_test']} годин")
    print(f"MAE: persistence {r['mae_persistence']}, seasonal_naive {r['mae_seasonal_naive']}, "
          f"climatology {r['mae_climatology']}, ridge {r['mae_ridge']}, hgb {r['mae_hgb']}")
    print(f"найкраща модель: {r['best_model']} (MAE {r['best_mae']}), "
          f"виграш проти найкращого базового: {r['gain_vs_best_baseline_c']} °C")
    print(f"printernya_temp: hgb MAE {r['printernya_mae_hgb']}, "
          f"persistence MAE {r['printernya_mae_persistence']}")


if __name__ == "__main__":
    main()
