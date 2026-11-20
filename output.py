"""输出：写盘、文字转曲、导出 PNG，以及整批生成流程。"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

import config
from config import Config
from signs import SignError, build_basename, render_svg, resolve

TIMEOUT = 180

OVERWRITE = "overwrite"
SKIP = "skip"
OVERWRITE_ALL = "overwrite_all"
SKIP_ALL = "skip_all"

Progress = Callable[[int, int, str, bool, str], None]
ConflictHook = Callable[[str, Path], str]


class ExportError(Exception):
    """写盘或调用 Inkscape 失败。"""


# ---------- Inkscape 处理 ----------

def _run(command: list[str], timeout: int = TIMEOUT) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, creationflags=config.NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExportError(f"Inkscape 处理超时（{timeout} 秒）") from exc
    except OSError as exc:
        raise ExportError(f"无法启动 Inkscape：{exc}") from exc


def _error_text(result: subprocess.CompletedProcess) -> str:
    return " ".join((result.stderr or result.stdout or "").split())[:200]


def svg_text_to_path(exe: str, svg_path: Path) -> None:
    """把 SVG 中的文字转为路径，成功后覆盖原文件。"""
    converted = svg_path.with_name(f"{svg_path.stem}.~convert.svg")
    result = _run([
        exe, str(svg_path),
        "--export-text-to-path",
        "--export-type=svg",
        f"--export-filename={converted}",
    ])
    if result.returncode != 0 or not converted.exists():
        converted.unlink(missing_ok=True)
        raise ExportError(f"文字转曲失败：{_error_text(result) or 'Inkscape 没有输出文件'}")

    # Inkscape 偶尔退出码为 0 却没真正转曲，回读确认 <text> 已清零
    if "<text" in converted.read_text(encoding="utf-8", errors="replace"):
        converted.unlink(missing_ok=True)
        raise ExportError("文字转曲未生效：输出文件仍包含 <text> 元素")
    os.replace(converted, svg_path)


def export_png(exe: str, svg_path: Path, png_path: Path, scale: int = 2) -> None:
    result = _run([
        exe, str(svg_path),
        "--export-type=png",
        f"--export-filename={png_path}",
        f"--export-dpi={96 * max(1, scale)}",
    ])
    if result.returncode != 0 or not png_path.exists() or png_path.stat().st_size == 0:
        png_path.unlink(missing_ok=True)
        raise ExportError(f"导出 PNG 失败：{_error_text(result) or 'Inkscape 没有输出 PNG'}")


def write_sign(svg_text: str, out_dir: Path, basename: str, cfg: Config, exe: str) -> list[Path]:
    """落盘一个标志，返回生成的文件列表；失败时清理残留。"""
    out_dir = Path(out_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ExportError(f"无法创建输出目录：{exc}") from exc

    want_svg, want_png = cfg.want_svg, cfg.want_png
    created: list[Path] = []
    temp_dir: str | None = None

    if want_svg:
        svg_path = out_dir / f"{basename}.svg"
    else:
        temp_dir = tempfile.mkdtemp(prefix="roadsign_")
        svg_path = Path(temp_dir) / f"{basename}.svg"

    try:
        try:
            svg_path.write_text(svg_text, encoding="utf-8")
        except OSError as exc:
            raise ExportError(f"无法写入文件：{exc}") from exc
        if want_svg:
            created.append(svg_path)

        # 只有 SVG 是交付物时才需要转曲；导出 PNG 时文字由渲染器直接栅格化
        if cfg.text_to_path and want_svg:
            if not exe:
                raise ExportError("文字转曲需要 Inkscape")
            svg_text_to_path(exe, svg_path)

        if want_png:
            if not exe:
                raise ExportError("导出 PNG 需要 Inkscape")
            png_path = out_dir / f"{basename}.png"
            export_png(exe, svg_path, png_path, cfg.png_scale)
            created.append(png_path)

        return created
    except Exception:
        for path in created:
            Path(path).unlink(missing_ok=True)
        raise
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


# ---------- 批量生成 ----------

@dataclass
class RowResult:
    row_no: int
    col1: str
    col2: str
    col3: str
    ok: bool
    files: tuple[Path, ...] = ()
    error: str = ""
    basename: str = ""
    skipped: bool = False


@dataclass
class Summary:
    results: list[RowResult] = field(default_factory=list)
    output_dir: str = ""
    elapsed: float = 0.0

    @property
    def ok_count(self) -> int:
        return sum(1 for item in self.results if item.ok)

    @property
    def skipped_count(self) -> int:
        return sum(1 for item in self.results if item.skipped)

    @property
    def failed(self) -> list[RowResult]:
        return [item for item in self.results if not item.ok and not item.skipped]

    @property
    def failed_count(self) -> int:
        return len(self.failed)


def inkscape_path(cfg: Config) -> str:
    """配置里没填时退回自动探测，避免手滑清空路径后整批失败。"""
    path = cfg.inkscape_path or ""
    if path and os.path.isfile(path):
        return path
    return config.detect_inkscape()


def preflight(cfg: Config) -> str:
    """生成前的环境检查：返回无法开始的原因，可以开始时返回空字符串。"""
    status_text, fonts_ok = config.font_status_text()
    if not fonts_ok:
        return (
            f"{status_text}。\n"
            "缺少专用字体。请安装A型、B型交通标志专用字体后重试。"
        )

    exe = inkscape_path(cfg)
    needs_inkscape = cfg.want_png or (cfg.text_to_path and cfg.want_svg)

    if needs_inkscape and not exe:
        if cfg.want_png:
            return "导出 PNG 需要 Inkscape，请先在上方指定 Inkscape 路径。"
        return "已开启文字转曲，但未找到 Inkscape。\n请指定 Inkscape 路径，或关闭「文字转曲」。"

    try:
        out_dir = Path(cfg.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        probe = out_dir / ".~write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return f"输出目录不可写：{exc}"

    return ""


def _existing_target(out_dir: Path, basename: str) -> Path | None:
    for suffix in (".svg", ".png"):
        candidate = out_dir / f"{basename}{suffix}"
        if candidate.exists():
            return candidate
    return None


def run(
    rows: Iterable[Sequence[str]],
    cfg: Config,
    progress: Progress | None = None,
    on_conflict: ConflictHook | None = None,
) -> Summary:
    """批量生成。progress(done, total, basename, ok, message) 用于界面反馈。"""
    row_list = list(rows)
    total = len(row_list)
    started = time.time()
    out_dir = Path(cfg.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    exe = inkscape_path(cfg)

    used: dict[str, int] = {}
    policy: str | None = None
    summary = Summary(output_dir=str(out_dir))

    def decide(basename: str, existing: Path) -> str:
        nonlocal policy
        if policy:
            return policy
        if on_conflict is None:
            return SKIP
        answer = on_conflict(basename, existing)
        if answer == OVERWRITE_ALL:
            policy = OVERWRITE
            return OVERWRITE
        if answer == SKIP_ALL:
            policy = SKIP
            return SKIP
        return OVERWRITE if answer == OVERWRITE else SKIP

    for index, row in enumerate(row_list, start=1):
        cells = [str(cell).strip() for cell in (list(row) + ["", "", ""])[:3]]
        col1, col2, col3 = cells
        try:
            spec = resolve(col1, col2, col3)
            svg_text = render_svg(spec)
        except SignError as exc:
            summary.results.append(RowResult(index, col1, col2, col3, False, error=str(exc)))
            if progress:
                progress(index, total, "", False, str(exc))
            continue

        basename = build_basename(spec, cfg.naming)
        seen = used.get(basename, 0)
        used[basename] = seen + 1
        if seen:
            basename = f"{basename} ({seen + 1})"

        existing = _existing_target(out_dir, basename)
        if existing and decide(basename, existing) == SKIP:
            message = "已存在同名文件，已跳过"
            summary.results.append(RowResult(
                index, col1, col2, col3, False, error=message, basename=basename, skipped=True,
            ))
            if progress:
                progress(index, total, basename, False, message)
            continue

        try:
            files = write_sign(svg_text, out_dir, basename, cfg, exe)
        except ExportError as exc:
            summary.results.append(RowResult(
                index, col1, col2, col3, False, error=str(exc), basename=basename,
            ))
            if progress:
                progress(index, total, basename, False, str(exc))
            continue

        summary.results.append(RowResult(index, col1, col2, col3, True, tuple(files), "", basename))
        if progress:
            progress(index, total, basename, True, "")

    summary.elapsed = time.time() - started
    return summary
