"""配置读写与运行环境探测（Inkscape 路径、交通标志字体）。"""

from __future__ import annotations

import json
import os
import shutil
import string
import subprocess
import sys
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

if getattr(sys, "frozen", False):
    # 打包成 exe 后 __file__ 指向临时解包目录，配置要跟着 exe 放
    PROJECT_ROOT = Path(sys.executable).resolve().parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parent

CONFIG_PATH = PROJECT_ROOT / "config.json"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output"

NAMING_CHOICES = {"zh": "中文", "en": "英文"}
FORMAT_CHOICES = {"svg": "SVG", "png": "PNG", "both": "SVG + PNG"}

IS_WINDOWS = os.name == "nt"
NO_WINDOW = 0x08000000 if IS_WINDOWS else 0  # 调用子进程时不弹控制台窗口


@dataclass
class Config:
    output_dir: str = str(DEFAULT_OUTPUT_DIR)
    naming: str = "zh"            # zh | en
    formats: str = "svg"          # svg | png | both
    inkscape_path: str = ""
    text_to_path: bool = True     # 文字转曲
    png_scale: int = 2            # PNG 相对 SVG 尺寸的倍数

    @property
    def want_svg(self) -> bool:
        return self.formats in ("svg", "both")

    @property
    def want_png(self) -> bool:
        return self.formats in ("png", "both")

    def to_dict(self) -> dict:
        return asdict(self)


def _clean(cfg: Config) -> Config:
    if cfg.naming not in NAMING_CHOICES:
        cfg.naming = "zh"
    if cfg.formats not in FORMAT_CHOICES:
        cfg.formats = "svg"
    try:
        cfg.png_scale = max(1, min(int(cfg.png_scale), 8))
    except (TypeError, ValueError):
        cfg.png_scale = 2
    cfg.text_to_path = bool(cfg.text_to_path)
    return cfg


def load_config(path: Path = CONFIG_PATH) -> Config:
    """读取配置；没有配置文件或 Inkscape 路径失效时自动探测。"""
    data: dict = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}

    known = set(Config.__dataclass_fields__)
    cfg = _clean(Config(**{k: v for k, v in data.items() if k in known}))

    if not cfg.inkscape_path or not os.path.isfile(cfg.inkscape_path):
        cfg.inkscape_path = detect_inkscape()
    return cfg


def save_config(cfg: Config, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- Inkscape ----------

@lru_cache(maxsize=1)
def detect_inkscape() -> str:
    """PATH → 注册表 App Paths → 环境变量目录 → 各盘常见安装位置。"""
    candidates: list[str] = []

    found = shutil.which("inkscape")
    if found:
        candidates.append(found)
    candidates.extend(_registry_app_paths())

    for env_name, suffix in (
        ("ProgramFiles", os.path.join("Inkscape", "bin", "inkscape.exe")),
        ("ProgramFiles(x86)", os.path.join("Inkscape", "bin", "inkscape.exe")),
        ("LOCALAPPDATA", os.path.join("Programs", "Inkscape", "bin", "inkscape.exe")),
    ):
        base = os.environ.get(env_name)
        if base:
            candidates.append(os.path.join(base, suffix))

    for drive in string.ascii_uppercase[:12]:
        root = f"{drive}:\\"
        if not os.path.isdir(root):
            continue
        for sub in (
            r"Program Files\Inkscape\bin\inkscape.exe",
            r"Program Files (x86)\Inkscape\bin\inkscape.exe",
            r"Inkscape\bin\inkscape.exe",
            r"Programs\Inkscape\bin\inkscape.exe",
            r"Software\Inkscape\bin\inkscape.exe",
        ):
            candidates.append(root + sub)

    for path in candidates:
        try:
            if path and os.path.isfile(path):
                return path
        except OSError:
            continue
    return ""


def _registry_app_paths() -> list[str]:
    if not IS_WINDOWS:
        return []
    try:
        import winreg
    except ImportError:
        return []

    result: list[str] = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for sub in (
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\inkscape.exe",
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\inkscape.exe",
        ):
            try:
                with winreg.OpenKey(hive, sub) as key:
                    value, _ = winreg.QueryValueEx(key, "")
                    if value:
                        result.append(str(value).strip('"'))
            except OSError:
                continue
    return result


@lru_cache(maxsize=4)
def inkscape_version(exe: str) -> str:
    """Inkscape 版本号，取不到时返回空字符串。"""
    if not exe or not os.path.isfile(exe):
        return ""
    try:
        result = subprocess.run(
            [exe, "--version"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    lines = (result.stdout or "").strip().splitlines()
    if not lines:
        return ""
    parts = lines[0].replace("Inkscape", "").strip().split()
    return parts[0] if parts else ""


# ---------- 字体 ----------

@lru_cache(maxsize=1)
def check_fonts() -> dict[str, bool | None]:
    """返回 {"A": 是否安装, "B": 是否安装}；非 Windows 返回 None 表示无法判断。"""
    if not IS_WINDOWS:
        return {"A": None, "B": None}
    try:
        import winreg
    except ImportError:
        return {"A": None, "B": None}

    names: list[str] = []
    for hive, sub in (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows NT\CurrentVersion\Fonts"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
    ):
        try:
            with winreg.OpenKey(hive, sub) as key:
                count = winreg.QueryInfoKey(key)[1]
                for index in range(count):
                    try:
                        names.append(str(winreg.EnumValue(key, index)[0]))
                    except OSError:
                        break
        except OSError:
            continue

    joined = "\n".join(names)
    has_traffic_font = "交通标志" in joined
    return {"A": has_traffic_font and "A型" in joined, "B": has_traffic_font and "B型" in joined}


FONT_LABELS = {"A": "A型", "B": "B型"}


def font_status_text() -> tuple[str, bool]:
    """(状态文本, 是否齐全)。"""
    fonts = check_fonts()
    if fonts["A"] is None:
        return "字体检测不可用（非 Windows）", True

    missing = [FONT_LABELS[name] for name in ("A", "B") if not fonts[name]]
    if not missing:
        return "A型、B型交通标志专用字体已安装", True
    if len(missing) == 2:
        return "未安装交通标志专用字体", False
    return f"缺少 {missing[0]}交通标志专用字体", False
