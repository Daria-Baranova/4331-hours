"""Модуль M9 — Excel-звіт для людини (проєкт «4331 година»).

Питання: **як показати результат людині, яка не відкриває ноутбуки?**

Дані в `excel/report.xlsx` кладе Python (openpyxl), формули прописує Claude —
так само як буде на реальній роботі: аналітик готує таблицю, формули й
калькулятор рахує сам Excel, а не Python. Це самодостатній файл: підсумкові
числа не переносяться з results/ як текст, а рахуються формулами `AVERAGEIFS`,
`SUMIFS`, `COUNTIFS`, `LN` прямо в Excel над аркушем `Data`.

⚠️ Excel на цій машині uk-UA (роздільник аргументів `;`, десяткова кома `,`),
але openpyxl завжди пише формули у **файловому** форматі: англійські назви
функцій і кома як роздільник аргументів. Excel сам показує їх локалізовано —
у файлі коми лишаються комами. Тому в цьому модулі немає жодного `;` у
жодному рядку формули.

Аркуші (у цьому порядку):
  1. `Data`         — погодинні дані з `data/clean/hours_wide.csv` як таблиця Excel;
  2. `Blackout_Log` — журнал відключень з `results/m2_outages.csv` + зведення;
  3. `Dashboard`    — зведені таблиці формулами над `Data` + 4 діаграми;
  4. `Comfort`      — теплова карта «кімната × година доби», умовне форматування;
  5. `Cooling_Calc` — калькулятор охолодження (`LN`/`EXP`), захист аркуша.

Запуск: `PYTHONUTF8=1 python -m src.excel_report` → пише `excel/report.xlsx`.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.formatting.rule import ColorScaleRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.styles.protection import Protection
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.workbook.defined_name import DefinedName

ROOT = Path(__file__).resolve().parents[1]
DATA_CLEAN = ROOT / "data" / "clean"
RESULTS = ROOT / "results"
EXCEL_DIR = ROOT / "excel"
OUT_PATH = EXCEL_DIR / "report.xlsx"

ROOMS_UA = {"Bedroom": "Кімната", "Kitchen": "Кухня", "Printernya": "Кладова"}
ROOM_LIST_UA = ["Кімната", "Кухня", "Кладова"]
SOURCE_UA = {"hourly": "погодинні", "detailed": "детальні"}

# ── стилі, що повторюються ──────────────────────────────────────────────────
NUM_DATE = "dd.mm.yyyy hh:mm"
NUM_TEMP = "0.0"
NUM_HOUR = "0"
NUM_RAW_PCT = '0.0"%"'   # значення вже у шкалі 0..100 — просто підпис, без множення
NUM_FRAC_PCT = "0.0%"    # справжній дріб 0..1, отриманий формулою
NUM_K = "0.000000"

FILL_YELLOW = PatternFill("solid", fgColor="FFF2CC")
FILL_HEADER = PatternFill("solid", fgColor="2E86AB")
FILL_MARK = PatternFill("solid", fgColor="F5F0E6")
FONT_HEADER = Font(bold=True, color="FFFFFF")
FONT_TITLE = Font(bold=True, size=14, color="2E86AB")
FONT_BOLD = Font(bold=True)
FONT_ITALIC_GRAY = Font(italic=True, color="666666")
THIN = Side(style="thin", color="B7B7B7")
BORDER_ALL = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEFT_WRAP = Alignment(horizontal="left", vertical="top", wrap_text=True)


def style_header_row(ws, row: int, first_col: int, last_col: int) -> None:
    for c in range(first_col, last_col + 1):
        cell = ws.cell(row=row, column=c)
        cell.font = FONT_HEADER
        cell.fill = FILL_HEADER
        cell.alignment = CENTER
        cell.border = BORDER_ALL


# ── завантаження вхідних даних ───────────────────────────────────────────────
def _kyiv_naive(ts: str) -> datetime:
    """Парсить ISO-рядок з офсетом і повертає наївний datetime (київський час)."""
    return datetime.fromisoformat(ts).replace(tzinfo=None)


def load_hours_wide() -> list[dict]:
    rows = []
    with open(DATA_CLEAN / "hours_wide.csv", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            def num(key):
                v = r.get(key, "")
                return float(v) if v not in ("", None) else None

            rows.append(
                {
                    "dt": _kyiv_naive(r["ts_kyiv"]),
                    "bedroom_temp": num("bedroom_temp"),
                    "kitchen_temp": num("kitchen_temp"),
                    "printernya_temp": num("printernya_temp"),
                    "out_temp": num("out_temp"),
                    "bedroom_hum": num("bedroom_hum"),
                    "kitchen_hum": num("kitchen_hum"),
                    "printernya_hum": num("printernya_hum"),
                    "out_hum": num("out_hum"),
                }
            )
    return rows


def load_outages() -> list[dict]:
    with open(RESULTS / "m2_outages.csv", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def load_json(name: str) -> dict:
    with open(RESULTS / name, encoding="utf-8") as f:
        return json.load(f)


def load_daily_cycle() -> list[dict]:
    with open(RESULTS / "m3_daily_cycle.csv", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def load_comfort_monthly() -> dict[tuple[str, str], float]:
    """(month, room) -> temp_ok_pct, лише помісячні рядки (не 'усе')."""
    out = {}
    with open(RESULTS / "m3_comfort.csv", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if r["month"] != "усе":
                out[(r["month"], r["room"])] = float(r["temp_ok_pct"])
    return out


# ── аркуш 1: Data ─────────────────────────────────────────────────────────────
def build_data_sheet(wb: Workbook, hours: list[dict]) -> list[str]:
    ws = wb.active
    ws.title = "Data"

    headers = [
        "Дата-час (Київ)", "Місяць", "Година",
        "Кімната °C", "Кухня °C", "Кладова °C", "Надворі °C",
        "Кімната RH", "Кухня RH", "Кладова RH", "Надворі RH",
    ]
    ws.append(headers)
    style_header_row(ws, 1, 1, len(headers))

    # УВАГА: column_dimensions[...].number_format НЕ застосовується до клітинок,
    # які вже мають власне значення (Excel бере колонковий формат лише для
    # порожніх, "незайманих" клітинок) — перевірено через Excel COM. Тому формат
    # ставимо на кожну клітинку окремо, а не на колонку в цілому.
    months_seen: set[str] = set()
    for i, r in enumerate(hours, start=2):
        dt_ = r["dt"]
        months_seen.add(dt_.strftime("%Y-%m"))
        c1 = ws.cell(row=i, column=1, value=dt_)
        c1.number_format = NUM_DATE
        ws.cell(row=i, column=2, value=f'=TEXT(A{i},"yyyy-mm")')
        c3 = ws.cell(row=i, column=3, value=f"=HOUR(A{i})")
        c3.number_format = NUM_HOUR
        for col, key in ((4, "bedroom_temp"), (5, "kitchen_temp"),
                          (6, "printernya_temp"), (7, "out_temp")):
            cell = ws.cell(row=i, column=col, value=r[key])
            cell.number_format = NUM_TEMP
        for col, key in ((8, "bedroom_hum"), (9, "kitchen_hum"),
                          (10, "printernya_hum"), (11, "out_hum")):
            cell = ws.cell(row=i, column=col, value=r[key])
            cell.number_format = NUM_RAW_PCT

    last_row = len(hours) + 1

    widths = {"A": 18, "B": 10, "C": 8, "D": 11, "E": 11, "F": 11, "G": 11,
              "H": 11, "I": 11, "J": 11, "K": 11}
    for col, w in widths.items():
        ws.column_dimensions[col].width = w

    ws.freeze_panes = "A2"

    tab = Table(displayName="raw", ref=f"A1:K{last_row}")
    tab.tableStyleInfo = TableStyleInfo(
        name="TableStyleMedium2", showRowStripes=True,
        showFirstColumn=False, showLastColumn=False, showColumnStripes=False,
    )
    ws.add_table(tab)

    return sorted(months_seen)


# ── аркуш 2: Blackout_Log ────────────────────────────────────────────────────
def build_blackout_log(wb: Workbook, outages: list[dict], outages_json: dict) -> None:
    ws = wb.create_sheet("Blackout_Log")

    headers = ["№", "Початок (Київ)", "Кінець (Київ)", "Тривалість год",
               "День тижня", "Місяць", "Джерело", "Примітка"]
    ws.append(headers)
    style_header_row(ws, 1, 1, len(headers))

    n = len(outages)
    for i, ev in enumerate(outages, start=2):
        ws.cell(row=i, column=1, value=i - 1)
        cb = ws.cell(row=i, column=2, value=_kyiv_naive(ev["start_kyiv"]))
        cb.number_format = NUM_DATE
        cc_ = ws.cell(row=i, column=3, value=_kyiv_naive(ev["end_kyiv"]))
        cc_.number_format = NUM_DATE
        cd = ws.cell(row=i, column=4, value=float(ev["hours"]))
        cd.number_format = NUM_TEMP
        ws.cell(row=i, column=5, value=f'=TEXT(B{i},"dddd")')
        ws.cell(row=i, column=6, value=f'=TEXT(B{i},"yyyy-mm")')
        ws.cell(row=i, column=7, value=SOURCE_UA.get(ev["source"], ev["source"]))
        ws.cell(row=i, column=8, value=ev["notes"])

    last_row = n + 1
    widths = {"A": 5, "B": 18, "C": 18, "D": 14, "E": 14, "F": 10, "G": 12, "H": 70}
    for col, w in widths.items():
        ws.column_dimensions[col].width = w
    for row in range(2, last_row + 1):
        ws.cell(row=row, column=8).alignment = LEFT_WRAP

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:H{last_row}"

    # зведення праворуч
    j, k = "J", "K"
    ws[f"{j}1"] = "Зведення по відключеннях"
    ws[f"{j}1"].font = FONT_BOLD
    ws[f"{j}2"] = "Кількість відключень"
    ws[f"{k}2"] = f"=COUNTA(A2:A{last_row})"
    ws[f"{j}3"] = "Сума годин без світла"
    ws[f"{k}3"] = f"=SUM(D2:D{last_row})"
    ws[f"{j}4"] = "Медіана тривалості, год"
    ws[f"{k}4"] = f"=MEDIAN(D2:D{last_row})"
    ws[f"{j}5"] = "Максимум, год"
    ws[f"{k}5"] = f"=MAX(D2:D{last_row})"
    ws[f"{j}6"] = "Всього годин у періоді"
    ws[f"{k}6"] = float(outages_json["total_calendar_hours"])
    ws[f"{j}7"] = "Uptime (частка часу зі світлом)"
    ws[f"{k}7"] = f"=1-{k}3/{k}6"
    ws[f"{k}7"].number_format = NUM_FRAC_PCT
    for r in (3, 4, 5):
        ws[f"{k}{r}"].number_format = NUM_TEMP
    ws.column_dimensions["J"].width = 34
    ws.column_dimensions["K"].width = 12


# ── аркуш 5 (будуємо перед Dashboard, бо Dashboard посилається на нього): Cooling_Calc
def build_cooling_calc(wb: Workbook, m4: dict) -> dict:
    """Повертає координати ключових клітинок для іменованих діапазонів і Dashboard."""
    ws = wb.create_sheet("Cooling_Calc")
    ws["A1"] = "Cooling_Calc — калькулятор охолодження квартири"
    ws["A1"].font = FONT_TITLE

    # довідник k з модуля M4 (нічний метод, night_decay_ols)
    ws["D2"] = "Кімната"
    ws["E2"] = "k, 1/год"
    style_header_row(ws, 2, 4, 5)
    ws["D3"] = "Кімната"
    ws["E3"] = m4["k_bedroom"]
    ws["D4"] = "Кухня"
    ws["E4"] = m4["k_kitchen"]
    ws["D5"] = "Кладова"
    ws["E5"] = m4["k_printernya"]
    for r in (3, 4, 5):
        ws[f"E{r}"].number_format = NUM_K
    ws["D6"] = "k з модуля M4 (метод «нічний спад», night_decay_ols)"
    ws["D6"].font = FONT_ITALIC_GRAY
    ws.merge_cells("D6:F6")

    # вхідні дані (жовті, розблоковані)
    ws["B3"] = "Вхідні дані"
    ws["B3"].font = FONT_BOLD
    ws["B4"] = "Кімната"
    ws["C4"] = "Кімната"
    ws["B5"] = "T0, °C (у кімнаті зараз)"
    ws["C5"] = float(m4["t0_default_c"])
    ws["B6"] = "T_вул, °C (надворі)"
    ws["C6"] = -10.0
    ws["B7"] = "поріг, °C"
    ws["C7"] = float(m4["threshold_c"])
    for r in (4, 5, 6, 7):
        c = ws[f"C{r}"]
        c.fill = FILL_YELLOW
        c.protection = Protection(locked=False)
        c.border = BORDER_ALL
    ws["C5"].number_format = NUM_TEMP
    ws["C6"].number_format = NUM_TEMP
    ws["C7"].number_format = NUM_TEMP

    ws["B9"] = "k обраної кімнати"
    ws["C9"] = "=INDEX($E$3:$E$5,MATCH(C4,$D$3:$D$5,0))"
    ws["C9"].number_format = NUM_K
    ws["B10"] = "Годин до порога"
    ws["C10"] = '=IFERROR(LN((C5-C6)/(C7-C6))/C9,"перевірте вхідні дані")'
    ws["C10"].number_format = NUM_TEMP
    ws["C10"].font = FONT_BOLD

    # УВАГА-1: НЕ CONCAT() — openpyxl пише "голу" формулу без префікса _xlfn.,
    # а CONCAT (функція 2016+) без нього дає #NAME? в Excel. "&" безпечний завжди.
    # УВАГА-2: формат-код усередині TEXT() — це рядкові ДАНІ, а не синтаксис
    # формули: Excel читає їх за локаллю користувача завжди (uk-UA -> кома як
    # десятковий роздільник), на відміну від number_format клітинки (той
    # зберігається en-US-стилем і сам перекладається під час показу). "0.0"
    # з крапкою тут дає #VALUE!, тому дробовий формат-код — лише "0,0".
    ws["B11"] = (
        '="Якщо надворі "&TEXT(C6,"0")&" °C, кімната охолоне з "&'
        'TEXT(C5,"0")&" до "&TEXT(C7,"0")&'
        '" °C приблизно за "&TEXT(C10,"0,0")&" год."'
    )
    ws.merge_cells("B11:G11")
    ws["B11"].alignment = LEFT_WRAP
    ws["B11"].font = FONT_BOLD

    ws["B13"] = (
        "Примітка: k оцінено на літніх даних, довірчий інтервал широкий — "
        "уточнимо взимку."
    )
    ws.merge_cells("B13:G13")
    ws["B13"].font = FONT_ITALIC_GRAY
    ws["B13"].alignment = LEFT_WRAP

    # довідкова таблиця (фіксовані T0/поріг, не залежить від інтерактивних клітинок вище)
    ws["B16"] = "Довідкова таблиця: T_вул від −20 до +10 (T0=22, поріг=18)"
    ws["B16"].font = FONT_BOLD
    ws.merge_cells("B16:E16")
    ws["B17"] = "T0 (довідка)"
    ws["C17"] = 22.0
    ws["D17"] = "поріг (довідка)"
    ws["E17"] = 18.0

    ref_headers_row = 19
    headers = ["T_вул, °C", "Кімната, год", "Кухня, год", "Кладова, год"]
    for c, h in enumerate(headers, start=1):
        ws.cell(row=ref_headers_row, column=c, value=h)
    style_header_row(ws, ref_headers_row, 1, len(headers))

    t_outs = [-20, -15, -10, -5, 0, 5, 10]
    first_ref_row = ref_headers_row + 1
    for i, t in enumerate(t_outs):
        row = first_ref_row + i
        ws.cell(row=row, column=1, value=t)
        ws.cell(row=row, column=1).number_format = NUM_HOUR
        for col, kname in ((2, "k_bedroom"), (3, "k_kitchen"), (4, "k_printernya")):
            formula = f'=IFERROR(LN(($C$17-$A{row})/($E$17-$A{row}))/{kname},"")'
            cell = ws.cell(row=row, column=col, value=formula)
            cell.number_format = NUM_TEMP
    last_ref_row = first_ref_row + len(t_outs) - 1

    widths = {"A": 12, "B": 26, "C": 12, "D": 16, "E": 12, "F": 12, "G": 12}
    for col, w in widths.items():
        ws.column_dimensions[col].width = w

    # захист аркуша: усе заблоковано, крім жовтих вхідних клітинок; без пароля
    ws.protection.sheet = True

    # data validation
    dv_room = DataValidation(type="list", formula1='"Кімната,Кухня,Кладова"', allow_blank=False)
    ws.add_data_validation(dv_room)
    dv_room.add(ws["C4"])

    dv_t0 = DataValidation(type="decimal", operator="between", formula1="-20", formula2="40",
                            allow_blank=False, showErrorMessage=True,
                            errorTitle="Поза діапазоном", error="T0 має бути між -20 і 40 °C")
    ws.add_data_validation(dv_t0)
    dv_t0.add(ws["C5"])

    dv_tvul = DataValidation(type="decimal", operator="between", formula1="-40", formula2="40",
                              allow_blank=False, showErrorMessage=True,
                              errorTitle="Поза діапазоном", error="T_вул має бути між -40 і 40 °C")
    ws.add_data_validation(dv_tvul)
    dv_tvul.add(ws["C6"])

    dv_porog = DataValidation(type="decimal", operator="lessThan", formula1="$C$5",
                               allow_blank=False, showErrorMessage=True,
                               errorTitle="Поріг завеликий", error="поріг має бути менше за T0")
    ws.add_data_validation(dv_porog)
    dv_porog.add(ws["C7"])

    # лінійна діаграма з довідкової таблиці
    chart = LineChart()
    chart.title = "Годин до 18°C залежно від температури надворі"
    chart.x_axis.title = "T надворі, °C"
    chart.y_axis.title = "Годин"
    chart.style = 10
    chart.width, chart.height = 18, 10
    data = Reference(ws, min_col=2, max_col=4, min_row=ref_headers_row, max_row=last_ref_row)
    cats = Reference(ws, min_col=1, min_row=first_ref_row, max_row=last_ref_row)
    chart.add_data(data, titles_from_data=True)
    chart.set_categories(cats)
    ws.add_chart(chart, f"B{last_ref_row + 3}")

    return {
        "k_bedroom_cell": "Cooling_Calc!$E$3",
        "k_kitchen_cell": "Cooling_Calc!$E$4",
        "k_printernya_cell": "Cooling_Calc!$E$5",
        "T0_cell": "Cooling_Calc!$C$5",
        "T_vul_cell": "Cooling_Calc!$C$6",
        "Porig_cell": "Cooling_Calc!$C$7",
        "ref_headers_row": ref_headers_row,
        "first_ref_row": first_ref_row,
        "last_ref_row": last_ref_row,
    }


def add_named_ranges(wb: Workbook, coords: dict) -> None:
    names = {
        "k_bedroom": coords["k_bedroom_cell"],
        "k_kitchen": coords["k_kitchen_cell"],
        "k_printernya": coords["k_printernya_cell"],
        "T0": coords["T0_cell"],
        "T_vul": coords["T_vul_cell"],
        "Porig": coords["Porig_cell"],
    }
    for name, ref in names.items():
        wb.defined_names[name] = DefinedName(name, attr_text=f"'{ref.split('!')[0]}'!{ref.split('!')[1]}")


# ── аркуш 3: Dashboard ───────────────────────────────────────────────────────
def build_dashboard(wb: Workbook, months: list[str], cooling_coords: dict, n_blackout_rows: int) -> None:
    ws = wb.create_sheet("Dashboard")
    ws["A1"] = "Dashboard — 4331 година"
    ws["A1"].font = FONT_TITLE

    room_cols_temp = {"Кімната": "Кімната °C", "Кухня": "Кухня °C",
                       "Кладова": "Кладова °C", "Надворі": "Надворі °C"}

    # Block 1: середня температура за місяць
    b1_title_row = 4
    ws.cell(row=b1_title_row, column=1, value="Середня температура за місяць, °C").font = FONT_BOLD
    ws.merge_cells(start_row=b1_title_row, start_column=1, end_row=b1_title_row, end_column=5)
    b1_header_row = b1_title_row + 1
    headers1 = ["Місяць", "Кімната", "Кухня", "Кладова", "Надворі"]
    for c, h in enumerate(headers1, start=1):
        ws.cell(row=b1_header_row, column=c, value=h)
    style_header_row(ws, b1_header_row, 1, len(headers1))
    b1_first = b1_header_row + 1
    for i, m in enumerate(months):
        row = b1_first + i
        ws.cell(row=row, column=1, value=m)
        for c, room in enumerate(["Кімната", "Кухня", "Кладова", "Надворі"], start=2):
            col_name = room_cols_temp[room]
            formula = f'=IFERROR(AVERAGEIFS(raw[{col_name}],raw[Місяць],$A{row}),"")'
            cell = ws.cell(row=row, column=c, value=formula)
            cell.number_format = NUM_TEMP
    b1_last = b1_first + len(months) - 1

    # Block 2: мінімум / максимум за весь період
    b2_title_row = 4
    ws.cell(row=b2_title_row, column=7, value="Мінімум / максимум за період, °C").font = FONT_BOLD
    ws.merge_cells(start_row=b2_title_row, start_column=7, end_row=b2_title_row, end_column=9)
    b2_header_row = b2_title_row + 1
    for c, h in zip((7, 8, 9), ["Кімната", "Мін", "Макс"]):
        ws.cell(row=b2_header_row, column=c, value=h)
    style_header_row(ws, b2_header_row, 7, 9)
    b2_first = b2_header_row + 1
    for i, room in enumerate(["Кімната", "Кухня", "Кладова", "Надворі"]):
        row = b2_first + i
        col_name = room_cols_temp[room]
        ws.cell(row=row, column=7, value=room)
        ws.cell(row=row, column=8, value=f"=MIN(raw[{col_name}])").number_format = NUM_TEMP
        ws.cell(row=row, column=9, value=f"=MAX(raw[{col_name}])").number_format = NUM_TEMP
    b2_last = b2_first + 3

    # Block 3: частка часу поза комфортом 20-24
    b3_title_row = b2_last + 2
    ws.cell(row=b3_title_row, column=7, value="Поза комфортом 20–24°C, % часу").font = FONT_BOLD
    ws.merge_cells(start_row=b3_title_row, start_column=7, end_row=b3_title_row, end_column=8)
    b3_header_row = b3_title_row + 1
    ws.cell(row=b3_header_row, column=7, value="Кімната")
    ws.cell(row=b3_header_row, column=8, value="% поза комфортом")
    style_header_row(ws, b3_header_row, 7, 8)
    b3_first = b3_header_row + 1
    for i, room in enumerate(["Кімната", "Кухня", "Кладова"]):
        row = b3_first + i
        col_name = room_cols_temp[room]
        ws.cell(row=row, column=7, value=room)
        formula = (
            f'=(COUNTIFS(raw[{col_name}],"<20")+COUNTIFS(raw[{col_name}],">24"))'
            f"/COUNT(raw[{col_name}])"
        )
        ws.cell(row=row, column=8, value=formula).number_format = NUM_FRAC_PCT
    b3_last = b3_first + 2

    # Block 4: години без світла за місяць (SUMIFS з Blackout_Log)
    b4_title_row = b1_last + 2
    ws.cell(row=b4_title_row, column=1, value="Години без світла за місяць").font = FONT_BOLD
    ws.merge_cells(start_row=b4_title_row, start_column=1, end_row=b4_title_row, end_column=2)
    b4_header_row = b4_title_row + 1
    ws.cell(row=b4_header_row, column=1, value="Місяць")
    ws.cell(row=b4_header_row, column=2, value="Години без світла")
    style_header_row(ws, b4_header_row, 1, 2)
    b4_first = b4_header_row + 1
    blk_last = n_blackout_rows + 1
    for i in range(len(months)):
        row = b4_first + i
        src_row = b1_first + i
        ws.cell(row=row, column=1, value=f"=$A{src_row}")
        formula = (
            f"=SUMIFS(Blackout_Log!$D$2:$D${blk_last},"
            f"Blackout_Log!$F$2:$F${blk_last},$A{row})"
        )
        ws.cell(row=row, column=2, value=formula).number_format = NUM_TEMP
    b4_last = b4_first + len(months) - 1

    for col, w in {"A": 10, "B": 11, "C": 11, "D": 11, "E": 11,
                    "F": 3, "G": 11, "H": 14, "I": 11}.items():
        ws.column_dimensions[col].width = w

    ws.freeze_panes = "A2"

    # ── діаграми ──────────────────────────────────────────────────────────
    anchor_row = max(b1_last, b2_last, b3_last, b4_last) + 3

    chart1 = LineChart()
    chart1.title = "Середня температура за місяць"
    chart1.x_axis.title = "Місяць"
    chart1.y_axis.title = "°C"
    chart1.style = 10
    chart1.width, chart1.height = 18, 10
    data1 = Reference(ws, min_col=2, max_col=5, min_row=b1_header_row, max_row=b1_last)
    cats1 = Reference(ws, min_col=1, min_row=b1_first, max_row=b1_last)
    chart1.add_data(data1, titles_from_data=True)
    chart1.set_categories(cats1)
    ws.add_chart(chart1, f"A{anchor_row}")

    chart2 = BarChart()
    chart2.type = "col"
    chart2.title = "Години без світла за місяць"
    chart2.x_axis.title = "Місяць"
    chart2.y_axis.title = "Годин"
    chart2.style = 11
    chart2.width, chart2.height = 18, 10
    data2 = Reference(ws, min_col=2, min_row=b4_header_row, max_row=b4_last)
    cats2 = Reference(ws, min_col=1, min_row=b4_first, max_row=b4_last)
    chart2.add_data(data2, titles_from_data=True)
    chart2.set_categories(cats2)
    ws.add_chart(chart2, f"A{anchor_row + 21}")

    chart3 = BarChart()
    chart3.type = "col"
    chart3.grouping = "clustered"
    chart3.title = "Годин до 18°C залежно від температури надворі (Cooling_Calc)"
    chart3.x_axis.title = "T надворі, °C"
    chart3.y_axis.title = "Годин"
    chart3.style = 12
    chart3.width, chart3.height = 18, 10
    cc = wb["Cooling_Calc"]
    data3 = Reference(cc, min_col=2, max_col=4,
                       min_row=cooling_coords["ref_headers_row"], max_row=cooling_coords["last_ref_row"])
    cats3 = Reference(cc, min_col=1,
                       min_row=cooling_coords["first_ref_row"], max_row=cooling_coords["last_ref_row"])
    chart3.add_data(data3, titles_from_data=True)
    chart3.set_categories(cats3)
    ws.add_chart(chart3, f"A{anchor_row + 42}")

    chart4 = BarChart()
    chart4.type = "col"
    chart4.title = "Частка часу поза комфортом 20–24°C"
    chart4.x_axis.title = "Кімната"
    chart4.y_axis.title = "% часу"
    chart4.style = 13
    chart4.width, chart4.height = 18, 10
    data4 = Reference(ws, min_col=8, min_row=b3_header_row, max_row=b3_last)
    cats4 = Reference(ws, min_col=7, min_row=b3_first, max_row=b3_last)
    chart4.add_data(data4, titles_from_data=True)
    chart4.set_categories(cats4)
    ws.add_chart(chart4, f"A{anchor_row + 63}")

    # ── місце під ручну зведену таблицю + зріз ──────────────────────────────
    # Заливки й об'єднання тут НЕ робимо: вони заважають поставити зведену.
    # Лишаємо один підпис і вільні клітинки нижче.
    mark_row = anchor_row + 85
    mark_cell = ws.cell(row=mark_row, column=1)
    mark_cell.value = "Місце для зведеної таблиці та зрізу (робиться вручну, див. README)"
    mark_cell.font = Font(italic=True, size=11, color="8A6D00")


# ── аркуш 4: Comfort ─────────────────────────────────────────────────────────
def build_comfort(wb: Workbook, daily_cycle: list[dict], months: list[str],
                   comfort_monthly: dict[tuple[str, str], float]) -> None:
    ws = wb.create_sheet("Comfort")
    ws["A1"] = "Comfort — теплова карта"
    ws["A1"].font = FONT_TITLE

    # Heatmap 1: кімната x година доби
    h1_header_row = 3
    headers = ["Година", "Кімната", "Кухня", "Кладова", "Надворі"]
    for c, h in enumerate(headers, start=1):
        ws.cell(row=h1_header_row, column=c, value=h)
    style_header_row(ws, h1_header_row, 1, len(headers))
    h1_first = h1_header_row + 1
    src_cols = {"Кімната": "Bedroom", "Кухня": "Kitchen",
                "Кладова": "Printernya", "Надворі": "Outdoor"}
    for i, r in enumerate(daily_cycle):
        row = h1_first + i
        ws.cell(row=row, column=1, value=int(r["hour"]))
        for c, room in enumerate(["Кімната", "Кухня", "Кладова", "Надворі"], start=2):
            val = r[src_cols[room]]
            cell = ws.cell(row=row, column=c, value=float(val) if val != "" else None)
            cell.number_format = NUM_TEMP
    h1_last = h1_first + len(daily_cycle) - 1

    for col in (2, 3, 4, 5):
        letter = get_column_letter(col)
        rule = ColorScaleRule(
            start_type="min", start_color="4472C4",
            mid_type="percentile", mid_value=50, mid_color="FFFFFF",
            end_type="max", end_color="C0504D",
        )
        ws.conditional_formatting.add(f"{letter}{h1_first}:{letter}{h1_last}", rule)

    # Heatmap 2: комфортна температура за місяць по кімнатах, %
    h2_title_row = h1_last + 3
    ws.cell(row=h2_title_row, column=1,
            value="Комфортна температура 20–24°C за місяць, % часу").font = FONT_BOLD
    ws.merge_cells(start_row=h2_title_row, start_column=1, end_row=h2_title_row, end_column=4)
    h2_header_row = h2_title_row + 1
    headers2 = ["Місяць", "Кімната", "Кухня", "Кладова"]
    for c, h in enumerate(headers2, start=1):
        ws.cell(row=h2_header_row, column=c, value=h)
    style_header_row(ws, h2_header_row, 1, len(headers2))
    h2_first = h2_header_row + 1
    room_key = {"Кімната": "Bedroom", "Кухня": "Kitchen", "Кладова": "Printernya"}
    for i, m in enumerate(months):
        row = h2_first + i
        ws.cell(row=row, column=1, value=m)
        for c, room in enumerate(["Кімната", "Кухня", "Кладова"], start=2):
            val = comfort_monthly.get((m, room_key[room]))
            cell = ws.cell(row=row, column=c, value=val)
            cell.number_format = NUM_RAW_PCT
    h2_last = h2_first + len(months) - 1

    rule2 = ColorScaleRule(
        start_type="min", start_color="C0504D",
        mid_type="percentile", mid_value=50, mid_color="FFEB84",
        end_type="max", end_color="70AD47",
    )
    ws.conditional_formatting.add(f"B{h2_first}:D{h2_last}", rule2)

    note_row = h2_last + 2
    ws.cell(row=note_row, column=1,
            value=("Спарклайни (мінідіаграми в комірках) openpyxl не підтримує — "
                   "можна додати вручну: виділити рядок → Вставлення → Спарклайн → Лінія."))
    ws.merge_cells(start_row=note_row, start_column=1, end_row=note_row, end_column=6)
    ws.cell(row=note_row, column=1).font = FONT_ITALIC_GRAY
    ws.cell(row=note_row, column=1).alignment = LEFT_WRAP

    widths = {"A": 12, "B": 11, "C": 11, "D": 11, "E": 11}
    for col, w in widths.items():
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "B4"


# ── збірка й перевірка ────────────────────────────────────────────────────────
def build_workbook() -> tuple[Workbook, dict]:
    hours = load_hours_wide()
    outages = load_outages()
    outages_json = load_json("m2_outages.json")
    daily_cycle = load_daily_cycle()
    comfort_monthly = load_comfort_monthly()
    m4 = load_json("m4_cooling.json")

    wb = Workbook()
    months = build_data_sheet(wb, hours)
    build_blackout_log(wb, outages, outages_json)
    cooling_coords = build_cooling_calc(wb, m4)
    add_named_ranges(wb, cooling_coords)
    build_dashboard(wb, months, cooling_coords, n_blackout_rows=len(outages))
    build_comfort(wb, daily_cycle, months, comfort_monthly)

    # порядок аркушів: Data, Blackout_Log, Dashboard, Comfort, Cooling_Calc
    wb._sheets = [wb["Data"], wb["Blackout_Log"], wb["Dashboard"],
                  wb["Comfort"], wb["Cooling_Calc"]]
    wb.active = 0

    info = {"n_hours": len(hours), "n_outages": len(outages), "months": months, "m4": m4}
    return wb, info


def verify_static(path: Path) -> list[str]:
    """Перевірка без Excel: переоткриваємо файл і дивимось, що формули на місці."""
    notes = []
    wb2 = load_workbook(path)
    checks = [
        ("Data", "B2", "=TEXT"),
        ("Dashboard", None, None),
        ("Cooling_Calc", "C10", "=IFERROR(LN"),
    ]
    ws = wb2["Data"]
    v = ws["B2"].value
    notes.append(f"Data!B2 = {v!r} (очікували формулу TEXT)")
    assert isinstance(v, str) and v.startswith("=TEXT"), "Data!B2 має бути формулою"

    ws = wb2["Cooling_Calc"]
    v = ws["C10"].value
    notes.append(f"Cooling_Calc!C10 = {v!r}")
    assert isinstance(v, str) and v.startswith("=IFERROR(LN"), "формула калькулятора відсутня"
    v9 = ws["C9"].value
    notes.append(f"Cooling_Calc!C9 = {v9!r}")

    ws = wb2["Dashboard"]
    found_avgifs = any(
        isinstance(cell.value, str) and "AVERAGEIFS" in cell.value
        for row in ws.iter_rows() for cell in row
    )
    found_sumifs = any(
        isinstance(cell.value, str) and "SUMIFS" in cell.value
        for row in ws.iter_rows() for cell in row
    )
    found_countifs = any(
        isinstance(cell.value, str) and "COUNTIFS" in cell.value
        for row in ws.iter_rows() for cell in row
    )
    notes.append(f"Dashboard: AVERAGEIFS присутня={found_avgifs}, SUMIFS={found_sumifs}, COUNTIFS={found_countifs}")
    assert found_avgifs and found_sumifs and found_countifs

    assert "raw" in wb2["Data"].tables, "таблиця raw відсутня"
    notes.append("таблиця raw присутня на Data")
    return notes


def verify_with_excel_com(path: Path) -> list[str] | None:
    try:
        import win32com.client  # noqa: F401
    except ImportError:
        return None

    import pythoncom
    pythoncom.CoInitialize()
    notes = []
    excel = win32com.client.DispatchEx("Excel.Application")
    excel.Visible = False
    excel.DisplayAlerts = False
    try:
        wb = excel.Workbooks.Open(str(path))
        excel.CalculateFullRebuild()
        cc = wb.Sheets("Cooling_Calc")
        c10 = cc.Range("C10").Value
        notes.append(f"Cooling_Calc!C10 (годин до порога, Bedroom @ -10°C) = {c10}")
        b11 = cc.Range("B11").Value
        notes.append(f"Cooling_Calc!B11 (речення) = {b11}")

        # Excel-помилки повертаються через COM як від'ємні цілі коди (VT_ERROR),
        # а не як рядки "#NAME?" — тому рядковий .startswith("#") їх не ловить.
        error_codes = {
            -2146826281: "#NULL!", -2146826273: "#DIV/0!", -2146826259: "#NAME?",
            -2146826252: "#N/A", -2146826246: "#REF!", -2146826265: "#VALUE!",
            -2146826288: "#NUM!",
        }
        n_errors = 0
        for sheet in wb.Sheets:
            used = sheet.UsedRange
            vals = used.Value
            if vals is None:
                continue
            rows = vals if isinstance(vals[0], tuple) else (vals,)
            for row in rows:
                cells = row if isinstance(row, tuple) else (row,)
                for v in cells:
                    if isinstance(v, int) and v in error_codes:
                        notes.append(f"УВАГА: {error_codes[v]} в {sheet.Name}")
                        n_errors += 1
        notes.append(f"Помилок формул на весь workbook: {n_errors}")
        wb.Close(SaveChanges=False)
    finally:
        excel.Quit()
    return notes


def main() -> None:
    EXCEL_DIR.mkdir(parents=True, exist_ok=True)
    wb, info = build_workbook()
    wb.save(OUT_PATH)
    print(f"Записано {OUT_PATH} ({info['n_hours']} годинних рядків, {info['n_outages']} відключень)")

    com_notes = verify_with_excel_com(OUT_PATH)
    if com_notes is not None:
        print("Перевірка через Excel COM:")
        for n in com_notes:
            print(" -", n)
    else:
        print("pywin32 недоступний — перевірка лише статична (формули присутні, обчислення не виконувалось).")
        for n in verify_static(OUT_PATH):
            print(" -", n)


if __name__ == "__main__":
    main()
