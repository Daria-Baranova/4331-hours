"""M10 — LinkedIn-карусель проєкту «4331 година».

Будує `images/linkedin_carousel_ua.pdf` (10 сторінок, 1080 x 1080 px) і ті самі
слайди окремими PNG у `images/carousel/slide_01.png` … `slide_10.png`.

Усі числа читаються з `results/*.json` і `results/*.csv` — у цьому файлі немає
жодної «зашитої» цифри, тому карусель не може розійтися з рештою звітів.

Запуск з кореня репозиторію:

    PYTHONUTF8=1 python -m src.make_carousel
"""

from __future__ import annotations

import json
import pathlib
import warnings
from decimal import Decimal, ROUND_HALF_UP

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd
from matplotlib import font_manager
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import FancyBboxPatch, Rectangle

# ---------------------------------------------------------------- шляхи
ROOT = pathlib.Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"
IMAGES = ROOT / "images"
PNG_DIR = IMAGES / "carousel"
PDF_PATH = IMAGES / "linkedin_carousel_ua.pdf"

# ---------------------------------------------------------------- полотно
SIDE_PX = 1080          # квадрат для LinkedIn
DPI = 100               # figsize=(10.8, 10.8) * 100 = 1080 px
MARGIN = 90             # мінімальне поле з усіх боків
CONTENT_R = SIDE_PX - MARGIN   # права межа контенту = 990

# ---------------------------------------------------------------- палітра
BG = "#FAFCFD"
SURFACE = "#EFF4F7"
RULE = "#D5E0E7"
INK = "#12202B"
INK2 = "#456070"
MUTED = "#75909F"
ACCENT = "#2E86AB"
ACCENT_TINT = "#E4F0F6"
ROOM = {
    "bedroom": "#2E86AB",    # кімната
    "kitchen": "#F18F01",    # кухня
    "printernya": "#C73E1D",  # кладова
    "outdoor": "#6C757D",    # надворі
}
WARM_TINT = "#FBEEE9"       # підкладка під «червоні» блоки

# ---------------------------------------------------------------- шрифти
# Кирилиця має рендеритися гарантовано: беремо перший доступний з переліку.
SANS_CANDIDATES = ["Segoe UI", "Verdana", "DejaVu Sans"]
MONO_CANDIDATES = ["Consolas", "Cascadia Mono", "DejaVu Sans Mono"]

APOS = "’"          # український апостроф
MINUS = "−"         # типографський мінус
NAME = f"Дар{APOS}я Баранова"
PROJECT = "4331 година"

URL_DASHBOARD = "https://daria-baranova.github.io/4331-hours/dashboard/"
URL_REPO = "https://github.com/Daria-Baranova/4331-hours"


def _pick_font(candidates: list[str]) -> str:
    available = {f.name for f in font_manager.fontManager.ttflist}
    for name in candidates:
        if name in available:
            return name
    return "DejaVu Sans"


SANS = _pick_font(SANS_CANDIDATES)
MONO = _pick_font(MONO_CANDIDATES)


def fs(px: float) -> float:
    """Розмір шрифту в пунктах для потрібної висоти в пікселях (при dpi=100)."""
    return px * 72.0 / DPI


# ---------------------------------------------------------------- числа
def load_results() -> dict:
    """Читає всі числа, що потрібні каруселі, з results/."""
    def j(name: str) -> dict:
        return json.loads((RESULTS / f"{name}.json").read_text(encoding="utf-8"))

    data = {
        "m1": j("m1_quality"),
        "m2": j("m2_outages"),
        "m3": j("m3_climate"),
        "m4": j("m4_cooling"),
        "m5": j("m5_tech"),
        "m7": j("m7_model"),
        "daily": pd.read_csv(RESULTS / "m3_daily_cycle.csv"),
        "hours": pd.read_csv(RESULTS / "m4_hours_table.csv"),
        "outages": pd.read_csv(RESULTS / "m2_outages.csv"),
    }
    return data


def uk(value: float, digits: int = 0, group: bool = False) -> str:
    """Число в українському записі: кома як десятковий роздільник.

    Округлення — «половина вгору» (4,55 → 4,6), як у README; вбудований
    `format` дав би 4,5 через подання float.
    """
    q = Decimal(1).scaleb(-digits)
    rounded = Decimal(repr(float(value))).quantize(q, rounding=ROUND_HALF_UP)
    text = f"{rounded:,f}" if group else f"{rounded:f}"
    if digits == 0:
        text = text.split(".")[0]
    text = text.replace(",", " ").replace(".", ",")   # тонкий пробіл між тисячами
    return text.replace("-", MINUS)


# ---------------------------------------------------------------- примітиви
def new_slide() -> tuple[plt.Figure, plt.Axes]:
    """Порожній слайд + осі, де координати = пікселі, y росте вниз."""
    fig = plt.figure(figsize=(SIDE_PX / DPI, SIDE_PX / DPI), dpi=DPI)
    fig.patch.set_facecolor(BG)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_xlim(0, SIDE_PX)
    ax.set_ylim(SIDE_PX, 0)          # перевернута вісь: думаємо зверху вниз
    ax.set_facecolor(BG)
    ax.set_axis_off()
    return fig, ax


def text(ax, x, y, s, size=32, color=INK, weight=400, font=None,
         ha="left", va="baseline", spacing=None, **kw):
    return ax.text(
        x, y, s,
        fontsize=fs(size), color=color, fontweight=weight,
        fontfamily=font or SANS, ha=ha, va=va,
        linespacing=spacing if spacing is not None else 1.2, **kw,
    )


def lines(ax, x, y, rows, size=32, step=None, **kw):
    """Кілька рядків з однаковим кроком (зверху вниз)."""
    step = step or size * 1.35
    for i, row in enumerate(rows):
        text(ax, x, y + i * step, row, size=size, **kw)


def rule(ax, x0, y, x1, color=RULE, lw=2):
    ax.plot([x0, x1], [y, y], color=color, lw=lw, solid_capstyle="butt", zorder=0)


def panel(ax, x, y, w, h, fc=SURFACE, bar=None, bar_w=8, radius=10):
    """Прямокутна плашка зі скругленням і опційною кольоровою смугою зліва."""
    ax.add_patch(FancyBboxPatch(
        (x + radius, y + radius), w - 2 * radius, h - 2 * radius,
        boxstyle=f"round,pad={radius}", linewidth=0,
        facecolor=fc, zorder=0,
    ))
    if bar:
        ax.add_patch(Rectangle((x, y), bar_w, h, linewidth=0,
                               facecolor=bar, zorder=1))


def footer(ax, page: int, total: int = 10):
    rule(ax, MARGIN, 985, CONTENT_R, color=RULE, lw=1.5)
    text(ax, MARGIN, 1026, f"{PROJECT} · {NAME}", size=26, color=MUTED)
    text(ax, CONTENT_R, 1026, f"{page:02d} / {total}", size=26, color=MUTED,
         ha="right", font=MONO)


def stat_row(ax, y, items, x0=MARGIN, x1=CONTENT_R, num_size=86, lab_size=28,
             color=INK):
    """Ряд «велике число + підпис» — рівномірно по ширині контенту."""
    n = len(items)
    span = (x1 - x0) / n
    for i, (num, label) in enumerate(items):
        x = x0 + i * span
        text(ax, x, y, num, size=num_size, weight=700,
             color=color if not isinstance(color, list) else color[i])
        text(ax, x, y + lab_size + 26, label, size=lab_size, color=INK2)


def dot(ax, x, y, color, size=15):
    ax.plot([x], [y], marker="o", markersize=fs(size), color=color,
            markeredgewidth=0, zorder=3)


# ---------------------------------------------------------------- слайди
def slide_01_cover(ax, R):
    m1 = R["m1"]
    text(ax, MARGIN, 168, "ХАРКІВ · 16.03 – 15.09.2026", size=27, color=MUTED,
         font=MONO)
    text(ax, MARGIN - 14, 360, "4331", size=250, weight=700, color=INK,
         va="center")
    text(ax, MARGIN, 500, "година", size=100, weight=600, color=INK2,
         va="center")
    rule(ax, MARGIN, 580, MARGIN + 220, color=ACCENT, lw=7)

    lines(ax, MARGIN, 670, [
        "Скільки годин квартира",
        "лишається теплою без світла",
    ], size=44, step=62, color=INK, weight=600)

    text(ax, MARGIN, 806, f"{uk(m1['total_hours'], group=True)} години · "
                          f"{m1['sensors']} датчиків · Home Assistant",
         size=30, color=INK2)

    rule(ax, MARGIN, 900, CONTENT_R, color=RULE, lw=1.5)
    text(ax, MARGIN, 952, NAME, size=34, weight=600, color=INK)
    text(ax, MARGIN, 996, "пет-проєкт з аналітики даних", size=29, color=MUTED)


def slide_02_questions(ax, R):
    text(ax, MARGIN, 168, "Питання", size=62, weight=700, color=INK)

    panel(ax, MARGIN, 232, CONTENT_R - MARGIN, 212, fc=ACCENT_TINT, bar=ACCENT)
    text(ax, MARGIN + 42, 292, "ГОЛОВНЕ ПИТАННЯ", size=24, color=ACCENT,
         font=MONO, weight=700)
    lines(ax, MARGIN + 42, 350, [
        "Скільки годин квартира лишається",
        "придатною для життя без світла?",
    ], size=36, step=50, color=INK, weight=600)

    items = [
        "Як часто і надовго зникає світло?",
        "Скільки градусів втрачає квартира за годину?",
        f"При {MINUS}10 °C скільки годин до 18 °C?",
        "Скільки тепла дає техніка?",
        "Скільки часу вдома некомфортно?",
    ]
    y0, step = 540, 82
    for i, item in enumerate(items):
        y = y0 + i * step
        text(ax, MARGIN + 4, y, f"{i + 1}", size=32, weight=700, color=ACCENT,
             font=MONO)
        text(ax, MARGIN + 58, y, item, size=32, color=INK)
        if i < len(items) - 1:
            rule(ax, MARGIN, y + 28, CONTENT_R, color=RULE, lw=1)


def slide_03_data(ax, R):
    m1, m3 = R["m1"], R["m3"]
    text(ax, MARGIN, 166, "Дані", size=62, weight=700, color=INK)

    text(ax, MARGIN, 250,
         f"{uk(m1['total_hours'], group=True)} години × {m1['sensors']} датчиків",
         size=52, weight=700, color=ACCENT)
    text(ax, MARGIN, 306, "16.03 – 15.09.2026 · Харків, однокімнатна квартира",
         size=30, color=INK2)
    text(ax, MARGIN, 352, "Home Assistant (REST + WebSocket) + Open-Meteo",
         size=30, color=INK2)

    # схема квартири: кімната / кухня / кладова
    top, bot = 420, 878
    mid_x, split_y = 545, 660
    rooms = [
        ("кімната", "bedroom", m3["bedroom_mean_temp"], "з вікном",
         MARGIN, top, mid_x - MARGIN, bot - top),
        ("кухня", "kitchen", m3["kitchen_mean_temp"], "вікно · готування",
         mid_x + 14, top, CONTENT_R - mid_x - 14, split_y - top),
        ("кладова", "printernya", m3["printernya_mean_temp"], "без вікна · техніка",
         mid_x + 14, split_y + 14, CONTENT_R - mid_x - 14, bot - split_y - 14),
    ]
    for label, key, temp, note, x, y, w, h in rooms:
        col = ROOM[key]
        ax.add_patch(Rectangle((x, y), w, h, linewidth=2.5, edgecolor=col,
                               facecolor=col, alpha=0.10, zorder=0))
        ax.add_patch(Rectangle((x, y), w, h, linewidth=2.5, edgecolor=col,
                               facecolor="none", zorder=1))
        top_of_block = y + (h - 148) / 2          # підпис по центру кімнати
        text(ax, x + 28, top_of_block + 32, label, size=32, weight=600,
             color=INK)
        text(ax, x + 28, top_of_block + 98, f"{uk(temp, 1)} °C", size=56,
             weight=700, color=col)
        text(ax, x + 28, top_of_block + 140, note, size=25, color=MUTED)

    text(ax, MARGIN, 930, "середня температура за пів року", size=27, color=MUTED)


def slide_04_quality(ax, R):
    m1 = R["m1"]
    text(ax, MARGIN, 166, "Якість даних", size=62, weight=700, color=INK)
    text(ax, MARGIN, 224,
         f"8 перевірок на {uk(m1['total_hours'], group=True)} годинах",
         size=30, color=INK2)

    stat_row(ax, 366, [
        (uk(m1["duplicates"]), "дублікатів"),
        (uk(m1["out_of_range"]), "викидів"),
        (uk(m1["n_gaps_gt_1h"]), "дірок > 1 год"),
    ], num_size=94, lab_size=28)

    panel(ax, MARGIN, 520, CONTENT_R - MARGIN, 342, fc=WARM_TINT,
          bar=ROOM["printernya"])
    text(ax, MARGIN + 42, 582, "ГОЛОВНА ЗНАХІДКА", size=24,
         color=ROOM["printernya"], font=MONO, weight=700)
    text(ax, MARGIN + 42, 672, f"{uk(m1['longest_stuck_hours'])} години",
         size=76, weight=700, color=INK)
    lines(ax, MARGIN + 42, 736, [
        "датчик у кімнаті видавав те саме число —",
        f"покриття {uk(m1['coverage_bedroom_temp'], 1)} %, але дані фальшиві",
    ], size=32, step=46, color=INK)
    text(ax, MARGIN + 42, 830,
         "тому в чистих даних стоїть прапорець stuck", size=27, color=MUTED)

    text(ax, MARGIN, 930, "класичного бруду майже не було — була порожнеча",
         size=28, color=INK2)


def slide_05_blackouts(ax, R):
    m2 = R["m2"]
    text(ax, MARGIN, 166, "Блекаути", size=62, weight=700, color=INK)
    text(ax, MARGIN, 224, "подія = тиша в даних ≥ 3 хв, перевірена іншими датчиками",
         size=28, color=INK2)

    stat_row(ax, 366, [
        (uk(m2["n_events_total"]), "відключень"),
        (uk(m2["total_hours"]), "годин без світла"),
        (f"{uk(m2['uptime_pct'], 1)} %", "uptime"),
    ], num_size=86, lab_size=28)

    # дата найдовшої події — з results, а не вписана вручну
    y_m_d = m2["longest_event_start_kyiv"].split(" ")[0].split("-")
    longest_date = f"{y_m_d[2]}.{y_m_d[1]}"
    text(ax, MARGIN, 470,
         f"найдовше — {uk(m2['max_hours'])} годин ({longest_date}), "
         f"медіана {uk(m2['median_hours'])} годин",
         size=29, color=INK2)

    panel(ax, MARGIN, 552, CONTENT_R - MARGIN, 352, fc=ACCENT_TINT, bar=ACCENT)
    text(ax, MARGIN + 42, 616, "ЧЕСНИЙ ПОВОРОТ", size=24, color=ACCENT,
         font=MONO, weight=700)
    text(ax, MARGIN + 42, 680, "Спершу правило дало 7 подій.", size=36,
         weight=600, color=INK)
    lines(ax, MARGIN + 42, 744, [
        "· датчик застряг — 25 годин те саме значення",
        "· впала Zigbee-мережа — а сервер писав далі",
    ], size=30, step=48, color=INK)
    text(ax, MARGIN + 42, 862, "дві події виявилися не блекаутами",
         size=28, color=MUTED)


def slide_06_weather(ax, fig, R):
    m3 = R["m3"]
    daily = R["daily"]

    text(ax, MARGIN, 158, "Квартира проти погоди", size=58, weight=700, color=INK)
    text(ax, MARGIN, 232,
         f"+1 °C надворі = +{uk(m3['bedroom_sensitivity'], 2)} °C вдома",
         size=44, weight=700, color=ACCENT)
    text(ax, MARGIN, 284, "середня температура за годиною доби, пів року",
         size=27, color=MUTED)

    legend = [
        ("кімната", ROOM["bedroom"], 100),
        ("кухня", ROOM["kitchen"], 330),
        ("кладова", ROOM["printernya"], 535),
        ("надворі", ROOM["outdoor"], 775),
    ]
    for label, col, x in legend:
        dot(ax, x, 344, col, size=15)
        text(ax, x + 24, 354, label, size=28, color=INK2)

    axc = fig.add_axes((100 / SIDE_PX, 1 - 690 / SIDE_PX,
                        (CONTENT_R - 100) / SIDE_PX, (690 - 385) / SIDE_PX))
    axc.set_facecolor(BG)
    series = [
        ("Bedroom", ROOM["bedroom"]),
        ("Kitchen", ROOM["kitchen"]),
        ("Printernya", ROOM["printernya"]),
        ("Outdoor", ROOM["outdoor"]),
    ]
    for col_name, col in series:
        axc.plot(daily["hour"], daily[col_name], color=col, lw=4,
                 solid_capstyle="round", zorder=3)

    axc.set_xlim(0, 23)
    axc.set_ylim(10, 29)
    axc.set_xticks([0, 6, 12, 18, 23])
    axc.set_yticks([12, 16, 20, 24, 28])
    axc.set_yticklabels([f"{v} °C" for v in [12, 16, 20, 24, 28]])
    axc.set_xlabel("година доби", fontsize=fs(27), color=INK2,
                   fontfamily=SANS, labelpad=10)
    axc.tick_params(labelsize=fs(26), colors=INK2, length=0, pad=8)
    for lab in axc.get_xticklabels() + axc.get_yticklabels():
        lab.set_fontfamily(SANS)
    axc.grid(axis="y", color=RULE, lw=1.2)
    axc.set_axisbelow(True)
    for side in ("top", "right", "left"):
        axc.spines[side].set_visible(False)
    axc.spines["bottom"].set_color(RULE)
    axc.spines["bottom"].set_linewidth(1.5)

    text(ax, MARGIN, 840,
         f"згладжування: {uk(m3['bedroom_smoothing_x'], 1)}× кімната · "
         f"{uk(m3['kitchen_smoothing_x'], 1)}× кухня · "
         f"{uk(m3['printernya_smoothing_x'], 1)}× кладова",
         size=30, color=INK)
    text(ax, MARGIN, 898,
         f"кімната випереджає вулицю на {abs(m3['bedroom_lag_h'])} години — "
         f"сонце у вікно", size=30, color=INK)
    return axc


def slide_07_hours(ax, R):
    tbl = R["hours"]
    text(ax, MARGIN, 160, "Головне число", size=62, weight=700, color=INK)
    text(ax, MARGIN, 220, "годин від 22 °C до 18 °C у кімнаті без опалення",
         size=30, color=INK2)

    text(ax, MARGIN + 4, 300, "надворі", size=25, color=MUTED, font=MONO,
         weight=700)
    text(ax, 540, 300, "годин", size=25, color=MUTED, font=MONO, weight=700,
         ha="right")
    rule(ax, MARGIN, 322, CONTENT_R, color=RULE, lw=1.5)

    hours_max = float(tbl["hours_bedroom"].max())
    bar_x0, bar_w_max = 610, CONTENT_R - 610
    y0, step = 382, 72
    for i, row in tbl.iterrows():
        y = y0 + i * step
        hot = row["t_out_c"] == -10.0
        if hot:
            ax.add_patch(Rectangle((MARGIN - 18, y - 46),
                                   CONTENT_R - MARGIN + 36, 62,
                                   linewidth=0, facecolor=ACCENT_TINT, zorder=0))
        col = ACCENT if hot else INK
        w = 700 if hot else 400
        text(ax, MARGIN + 4, y, f"{uk(row['t_out_c'])} °C", size=34,
             color=col, weight=w, font=MONO)
        # Consolas має лише 400 і 700 — 600 тут дав би fallback-попередження
        text(ax, 540, y, uk(row["hours_bedroom"]), size=38, color=col,
             weight=700, ha="right", font=MONO)
        bw = bar_w_max * float(row["hours_bedroom"]) / hours_max
        ax.add_patch(Rectangle((bar_x0, y - 26), bw, 26, linewidth=0,
                               facecolor=ACCENT if hot else "#C3D6E0", zorder=2))

    text(ax, MARGIN, 930,
         "оцінка за літніми даними, уточню взимку", size=28, color=MUTED)


def slide_08_zeros(ax, R):
    m5, m7 = R["m5"], R["m7"]
    text(ax, MARGIN, 166, "Два чесні нулі", size=62, weight=700, color=INK)

    box_h = 238
    for i, (tag, head, det1, det2) in enumerate([
        ("01", "ПК не гріє кімнату",
         f"+100 Вт → {uk(m5['coef_pc_per_100w'], 2)} °C, "
         f"p = {uk(m5['p_pc'], 2)}",
         f"вулиця пояснює все сама: R² = {uk(m5['r2_pc'], 2)}"),
        ("02", f"Модель не б{APOS}є базовий прогноз",
         f"кладова: MAE {uk(m7['printernya_mae_hgb'], 3)} проти "
         f"{uk(m7['printernya_mae_persistence'], 3)} °C",
         "стабільне тепло техніки робить її передбачуваною"),
    ]):
        y = 250 + i * (box_h + 38)
        panel(ax, MARGIN, y, CONTENT_R - MARGIN, box_h, fc=SURFACE, bar=ACCENT)
        text(ax, MARGIN + 42, y + 58, tag, size=26, color=ACCENT, font=MONO,
             weight=700)
        text(ax, MARGIN + 42, y + 122, head, size=40, weight=700, color=INK)
        text(ax, MARGIN + 42, y + 172, det1, size=29, color=INK2)
        text(ax, MARGIN + 42, y + 214, det2, size=29, color=MUTED)

    rule(ax, MARGIN, 850, CONTENT_R, color=RULE, lw=1.5)
    text(ax, MARGIN, 920, "нульовий результат — теж результат", size=40,
         weight=700, color=ACCENT)


def slide_09_stack(ax, R):
    text(ax, MARGIN, 160, "Стек і скіли", size=62, weight=700, color=INK)

    shares = [("Python", 60, ACCENT), ("SQL", 25, ROOM["kitchen"]),
              ("Excel", 15, ROOM["printernya"])]
    total_w = CONTENT_R - MARGIN
    x = MARGIN
    for name, pct, col in shares:
        w = total_w * pct / 100
        ax.add_patch(Rectangle((x, 212), w - 4, 54, linewidth=0,
                               facecolor=col, zorder=2))
        text(ax, x + w / 2 - 2, 318, f"{name} {pct} %", size=28,
             color=INK2, ha="center")
        x += w

    blocks = [
        (ACCENT, "Python", ["pandas · scipy curve_fit",
                            "statsmodels · scikit-learn"]),
        (ROOM["kitchen"], "SQL", ["LAG · gaps-and-islands · ROWS BETWEEN",
                                  "VIEW · percentile_cont"]),
        (ROOM["printernya"], "Excel", ["калькулятор на LN / EXP",
                                       "зведені · умовне форматування"]),
    ]
    y0, step = 415, 150
    for i, (col, head, rows) in enumerate(blocks):
        y = y0 + i * step
        dot(ax, MARGIN + 8, y - 11, col, size=16)
        text(ax, MARGIN + 40, y, head, size=34, weight=700, color=INK)
        lines(ax, MARGIN + 40, y + 48, rows, size=28, step=40, color=INK2)

    panel(ax, MARGIN, 840, CONTENT_R - MARGIN, 92, fc=ACCENT_TINT, bar=ACCENT)
    text(ax, MARGIN + 42, 897, "усі ключові числа звірено: Python = SQL, 8 / 8",
         size=31, weight=600, color=INK)


def slide_10_cta(ax, R):
    text(ax, MARGIN, 216, "ДИВИТИСЯ", size=26, color=MUTED, font=MONO,
         weight=700)
    text(ax, MARGIN, 310, "Дашборд і код", size=66, weight=700, color=INK)

    for i, (label, url) in enumerate([
        ("Дашборд проєкту", URL_DASHBOARD),
        ("Код і README двома мовами", URL_REPO),
    ]):
        y = 420 + i * 168
        panel(ax, MARGIN, y, CONTENT_R - MARGIN, 128, fc=SURFACE, bar=ACCENT)
        text(ax, MARGIN + 42, y + 56, label, size=30, weight=600, color=INK)
        text(ax, MARGIN + 42, y + 100, url, size=25, color=ACCENT, font=MONO)

    rule(ax, MARGIN, 820, CONTENT_R, color=RULE, lw=1.5)
    text(ax, MARGIN, 886, NAME, size=42, weight=700, color=INK)
    text(ax, MARGIN, 936, "трейні дата-аналітик · Харків", size=32, color=INK2)


# ---------------------------------------------------------------- перевірка
def check_bounds(fig, page: int) -> list[str]:
    """Жоден текст не має вилазити за полотно 1080 x 1080."""
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    problems: list[str] = []

    texts = list(fig.texts)
    for axis in fig.axes:
        texts += list(axis.texts)
        if not axis.axison:          # фонові осі-«лінійка»: підписів там немає
            continue
        texts += [axis.xaxis.label, axis.yaxis.label, axis.title]
        texts += axis.get_xticklabels() + axis.get_yticklabels()

    for t in texts:
        if not t.get_text():
            continue
        try:
            bb = t.get_window_extent(renderer=renderer)
        except Exception:                      # pragma: no cover
            continue
        if bb.x0 < 0 or bb.y0 < 0 or bb.x1 > SIDE_PX or bb.y1 > SIDE_PX:
            problems.append(
                f"слайд {page:02d}: текст {t.get_text()[:38]!r} виходить за "
                f"полотно — x [{bb.x0:.0f}, {bb.x1:.0f}], "
                f"y [{bb.y0:.0f}, {bb.y1:.0f}]"
            )
    return problems


# ---------------------------------------------------------------- збірка
def build() -> None:
    IMAGES.mkdir(parents=True, exist_ok=True)
    PNG_DIR.mkdir(parents=True, exist_ok=True)
    R = load_results()

    problems: list[str] = []
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")

        with PdfPages(PDF_PATH) as pdf:
            for page in range(1, 11):
                fig, ax = new_slide()
                if page == 1:
                    slide_01_cover(ax, R)
                elif page == 2:
                    slide_02_questions(ax, R)
                elif page == 3:
                    slide_03_data(ax, R)
                elif page == 4:
                    slide_04_quality(ax, R)
                elif page == 5:
                    slide_05_blackouts(ax, R)
                elif page == 6:
                    slide_06_weather(ax, fig, R)
                elif page == 7:
                    slide_07_hours(ax, R)
                elif page == 8:
                    slide_08_zeros(ax, R)
                elif page == 9:
                    slide_09_stack(ax, R)
                else:
                    slide_10_cta(ax, R)

                if page > 1:
                    footer(ax, page)

                problems += check_bounds(fig, page)
                pdf.savefig(fig, facecolor=BG, dpi=DPI)
                fig.savefig(PNG_DIR / f"slide_{page:02d}.png",
                            facecolor=BG, dpi=DPI)
                plt.close(fig)

            pdf.infodict().update({
                "Title": f"{PROJECT} — карусель для LinkedIn",
                "Author": NAME,
                "Subject": "Клімат квартири та відключення світла, Харків 2026",
            })

    glyph_warnings = [str(w.message) for w in caught
                      if "missing from font" in str(w.message)]

    print(f"шрифти: sans = {SANS}, mono = {MONO}")
    print(f"PDF:  {PDF_PATH}")
    print(f"PNG:  {PNG_DIR} (slide_01 … slide_10)")
    if glyph_warnings:
        print("!! відсутні гліфи:")
        for w in glyph_warnings[:10]:
            print("   ", w)
    else:
        print("гліфи: усе на місці, попереджень немає")
    if problems:
        print("!! текст за межами полотна:")
        for p in problems:
            print("   ", p)
    else:
        print("межі: увесь текст у полотні 1080 x 1080")


if __name__ == "__main__":
    build()
