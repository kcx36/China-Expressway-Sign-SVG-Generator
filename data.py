"""数据源：读取 xlsx / xls / csv，以及解析粘贴进来的文本。"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

from signs import to_text

Row = tuple[str, str, str]

_CELL_SPLIT = re.compile(r"[,，;；\t]")
_LINE_SPLIT = re.compile(r"[\r\n]+")
_NUMBER_LIKE = re.compile(r"^[GSXY][0-9A-Z]{3}$")
# 首行命中这些词就按表头跳过
_HEADER_WORDS = {"编号", "名称", "省份", "类型", "第一列", "高速编号", "高速名称", "路线名称"}


class DataSourceError(Exception):
    """数据源读取失败。"""


def read_table(path) -> list[Row]:
    """读取表格文件的前三列（首行是表头会自动跳过）。"""
    file_path = Path(path)
    if not file_path.is_file():
        raise DataSourceError(f"找不到文件：{file_path}")

    suffix = file_path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        raw = _read_xlsx(file_path)
    elif suffix == ".xls":
        raw = _read_xls(file_path)
    elif suffix in (".csv", ".txt"):
        raw = _read_csv(file_path)
    else:
        raise DataSourceError(f"不支持的文件类型：{suffix or '无扩展名'}（支持 xlsx / xls / csv）")

    rows: list[Row] = []
    header_checked = False
    for raw_row in raw:
        cells = [to_text(cell) for cell in (list(raw_row) + ["", "", ""])[:3]]
        if not any(cells):
            continue
        if not header_checked:
            header_checked = True
            if any(cell in _HEADER_WORDS for cell in cells):
                continue
        rows.append((cells[0], cells[1], cells[2]))
    return rows


def _read_xlsx(path: Path) -> list[list]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover
        raise DataSourceError("读取 xlsx 需要 openpyxl：pip install openpyxl") from exc
    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        raise DataSourceError(f"无法打开 Excel 文件：{exc}") from exc
    try:
        return [list(row) for row in workbook.worksheets[0].iter_rows(values_only=True)]
    finally:
        workbook.close()


def _read_xls(path: Path) -> list[list]:
    try:
        import xlrd
    except ImportError as exc:
        raise DataSourceError("读取 .xls 需要 xlrd：pip install xlrd（或另存为 .xlsx / .csv）") from exc
    try:
        sheet = xlrd.open_workbook(str(path)).sheet_by_index(0)
        return [sheet.row_values(index) for index in range(sheet.nrows)]
    except Exception as exc:
        raise DataSourceError(f"无法打开 .xls 文件：{exc}") from exc


def _read_csv(path: Path) -> list[list]:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "gbk", "utf-16"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise DataSourceError("无法识别 CSV 文件编码（请另存为 UTF-8 或 GBK）")

    sample = text[:4096]
    delimiter = "\t" if sample.count("\t") > sample.count(",") else ","
    return list(csv.reader(io.StringIO(text), delimiter=delimiter))


def _to_row(cells: list[str]) -> Row:
    parts = [cell.strip() for cell in cells if cell.strip()]
    if not parts:
        return ("", "", "")
    if len(parts) >= 3:
        return (parts[0], parts[1], parts[2])
    if len(parts) == 2:
        return (parts[0], parts[1], "")
    # 单列：支持 类型:编号:名称 的写法
    piece = parts[0]
    if ":" in piece or "：" in piece:
        sub = [part.strip() for part in re.split(r"[:：]", piece)]
        sub += [""] * (3 - len(sub))
        return (sub[0], sub[1], sub[2])
    return ("", piece, "")


def parse_pasted(text: str) -> list[Row]:
    """粘贴解析：每行一条记录，列用 Tab / 逗号分隔；单行逗号列表按编号处理。"""
    lines = [line for line in _LINE_SPLIT.split(text or "") if line.strip()]
    if not lines:
        return []
    if len(lines) == 1:
        parts = [part.strip() for part in _CELL_SPLIT.split(lines[0]) if part.strip()]
        if len(parts) > 1 and all(_NUMBER_LIKE.match(part) for part in parts):
            return [("", part, "") for part in parts]
    return [_to_row(_CELL_SPLIT.split(line)) for line in lines]
