"""标志：类型判定、编号校验、模板选择、SVG 渲染、文件命名。

「类型」列（表格第一列）规则：
    国家 / 国家高速 / 国高        -> 国家高速
    滇 / 滇高速                   -> 省级高速，前面的字符即省份简称
    国道 / 省道 / 县道 / 乡道       -> 对应等级的普通国省干线
    留空                        -> 按编号前缀判断普通国省干线

编号按严格格式校验，不做自动修正：
    普通国省干线   G/S/X/Y + 3 位（数字或字母），前缀必须与等级一致，如 G105、X201
    高速           1 个大写字母 + 1~4 位数字，如 G15、G0421、S27
不允许纯数字、中间空格、小写字母。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from xml.sax.saxutils import escape

from templates import EXPRESSWAY_TEMPLATES


class SignError(ValueError):
    """无法生成：输入不合法，或没有对应模板。"""


class SignKind:
    NATIONAL_EXPRESSWAY = "national_expressway"
    PROVINCIAL_EXPRESSWAY = "provincial_expressway"
    NATIONAL_ROAD = "national_road"
    PROVINCIAL_ROAD = "provincial_road"
    COUNTY_ROAD = "county_road"
    TOWNSHIP_ROAD = "township_road"


KIND_LABELS = {
    SignKind.NATIONAL_EXPRESSWAY: "国家高速",
    SignKind.PROVINCIAL_EXPRESSWAY: "省级高速",
    SignKind.NATIONAL_ROAD: "国道",
    SignKind.PROVINCIAL_ROAD: "省道",
    SignKind.COUNTY_ROAD: "县道",
    SignKind.TOWNSHIP_ROAD: "乡道",
}

EXPRESSWAY_KINDS = frozenset({SignKind.NATIONAL_EXPRESSWAY, SignKind.PROVINCIAL_EXPRESSWAY})
ROAD_KINDS = frozenset(
    {SignKind.NATIONAL_ROAD, SignKind.PROVINCIAL_ROAD, SignKind.COUNTY_ROAD, SignKind.TOWNSHIP_ROAD}
)

ROAD_PREFIX = {
    SignKind.NATIONAL_ROAD: "G",
    SignKind.PROVINCIAL_ROAD: "S",
    SignKind.COUNTY_ROAD: "X",
    SignKind.TOWNSHIP_ROAD: "Y",
}
PREFIX_KIND = {v: k for k, v in ROAD_PREFIX.items()}
TYPE_WORDS = {
    "国道": SignKind.NATIONAL_ROAD,
    "省道": SignKind.PROVINCIAL_ROAD,
    "县道": SignKind.COUNTY_ROAD,
    "乡道": SignKind.TOWNSHIP_ROAD,
}
NATIONAL_WORDS = {"国家", "国家高速", "国高"}
EXPRESSWAY_SUFFIX = "高速"

ROAD_NUMBER_RE = re.compile(r"^[GSXY][0-9A-Z]{3}$")
EXPRESSWAY_NUMBER_RE = re.compile(r"^[A-Z]\d{1,4}$")

# 省份简称：顺序即类型列下拉菜单的顺序
PROVINCES = (
    ("京", "北京", "Beijing"),
    ("津", "天津", "Tianjin"),
    ("冀", "河北", "Hebei"),
    ("晋", "山西", "Shanxi"),
    ("蒙", "内蒙古", "Inner Mongolia"),
    ("辽", "辽宁", "Liaoning"),
    ("吉", "吉林", "Jilin"),
    ("黑", "黑龙江", "Heilongjiang"),
    ("沪", "上海", "Shanghai"),
    ("苏", "江苏", "Jiangsu"),
    ("浙", "浙江", "Zhejiang"),
    ("皖", "安徽", "Anhui"),
    ("闽", "福建", "Fujian"),
    ("赣", "江西", "Jiangxi"),
    ("鲁", "山东", "Shandong"),
    ("豫", "河南", "Henan"),
    ("鄂", "湖北", "Hubei"),
    ("湘", "湖南", "Hunan"),
    ("粤", "广东", "Guangdong"),
    ("桂", "广西", "Guangxi"),
    ("琼", "海南", "Hainan"),
    ("渝", "重庆", "Chongqing"),
    ("川", "四川", "Sichuan"),
    ("黔", "贵州", "Guizhou"),
    ("滇", "云南", "Yunnan"),
    ("藏", "西藏", "Tibet"),
    ("陕", "陕西", "Shaanxi"),
    ("甘", "甘肃", "Gansu"),
    ("青", "青海", "Qinghai"),
    ("宁", "宁夏", "Ningxia"),
    ("新", "新疆", "Xinjiang"),
)

# 其他常见简称一律折算成上表的写法，避免出现「贵高速」「云高速」
PROVINCE_ALIASES = {"贵": "黔", "云": "滇", "蜀": "川"}

PROVINCE_ENGLISH = {abbr: english for abbr, _name, english in PROVINCES}


def to_text(value) -> str:
    """把表格单元格统一成字符串（None / NaN / 数字都能吃），只去掉首尾空白。"""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip()
    if text.lower() in {"nan", "none", "nat"}:
        return ""
    if text.endswith(".0") and text[:-2].isdigit():
        return text[:-2]
    return text


@dataclass(frozen=True)
class SignSpec:
    """一个待生成的标志。"""

    kind: str
    number: str
    name: str = ""
    province: str = ""

    @property
    def label(self) -> str:
        """省级高速带上省份，如「滇高速」。"""
        if self.kind == SignKind.PROVINCIAL_EXPRESSWAY and self.province:
            return f"{self.province}{EXPRESSWAY_SUFFIX}"
        return KIND_LABELS[self.kind]

    @property
    def digit_count(self) -> int:
        """编号去掉前导字母后的位数（G15 -> 2、G0421 -> 4）。"""
        return max(len(self.number) - 1, 1)

    @property
    def title(self) -> str:
        return " · ".join(part for part in (self.label, self.number, self.name) if part)


# ---------- 判定 ----------

def _check_road_number(kind: str, number: str) -> None:
    prefix = ROAD_PREFIX[kind]
    if not ROAD_NUMBER_RE.match(number):
        raise SignError(
            f"编号格式错误：{KIND_LABELS[kind]}应为 {prefix} + 3 位字符"
            f"（如 {prefix}105），当前「{number}」"
        )
    if number[0] != prefix:
        raise SignError(
            f"编号与类型不符：{KIND_LABELS[kind]}的编号应以 {prefix} 开头，当前「{number}」"
        )


def _check_expressway_number(number: str) -> None:
    if not EXPRESSWAY_NUMBER_RE.match(number):
        raise SignError(
            "编号格式错误：高速编号应为 1 个大写字母 + 1~4 位数字"
            f"（如 G15、G0421），当前「{number}」"
        )


def resolve(col_type, col_number, col_name="") -> SignSpec:
    """把三列文本解析为 SignSpec；任何不合规都会抛 SignError。"""
    first = to_text(col_type)
    number = to_text(col_number)
    name = to_text(col_name)

    if not number:
        raise SignError("编号为空")

    province = ""

    if not first:
        if len(number) == 4 and number[0] in PREFIX_KIND:
            kind = PREFIX_KIND[number[0]]
        else:
            raise SignError(
                f"类型为空：编号需为 G/S/X/Y + 3 位字符（如 G105），当前「{number}」"
            )
    elif first in NATIONAL_WORDS:
        kind = SignKind.NATIONAL_EXPRESSWAY
    elif first in TYPE_WORDS:
        kind = TYPE_WORDS[first]
    elif len(first) == 1:
        # 单字符一律视为省级高速：汉字简称，也允许 X 之类的自定义字符
        kind = SignKind.PROVINCIAL_EXPRESSWAY
        province = PROVINCE_ALIASES.get(first, first)
    elif len(first) == 3 and first.endswith(EXPRESSWAY_SUFFIX):
        # 「滇高速」这类写法：省份简称 + 高速
        kind = SignKind.PROVINCIAL_EXPRESSWAY
        province = PROVINCE_ALIASES.get(first[0], first[0])
    else:
        raise SignError(
            f"类型无法识别：「{first}」应填 国家高速、滇（或滇高速）、"
            f"国道/省道/县道/乡道，或留空由编号前缀判断"
        )

    if kind in ROAD_KINDS:
        _check_road_number(kind, number)
    else:
        _check_expressway_number(number)

    return SignSpec(kind=kind, number=number, name=name, province=province)


def try_resolve(col_type, col_number, col_name="") -> tuple[SignSpec | None, str]:
    """resolve 的非异常版本：(spec, 错误信息)。"""
    try:
        return resolve(col_type, col_number, col_name), ""
    except SignError as exc:
        return None, str(exc)


# ---------- 模板与校验 ----------

def _prefix(kind: str) -> str:
    return "national" if kind == SignKind.NATIONAL_EXPRESSWAY else "provincial"


def expressway_key(spec: SignSpec) -> str:
    tail = f"{len(spec.name)}char" if spec.name else "noname"
    return f"{_prefix(spec.kind)}_{spec.digit_count}digit_{tail}"


def supported_name_lengths(kind: str, digits: int) -> list[str]:
    """该"类型 + 编号位数"组合下，模板支持哪些名称长度（用于提示用户）。"""
    pattern = re.compile(rf"^{_prefix(kind)}_{digits}digit_(noname|(\d+)char)$")
    found = []
    for key in EXPRESSWAY_TEMPLATES:
        match = pattern.match(key)
        if not match:
            continue
        found.append((0, "无名称") if key.endswith("noname") else (int(match.group(2)), f"{match.group(2)} 字"))
    return [label for _, label in sorted(found)]


def validate(spec: SignSpec) -> str:
    """返回无法生成的原因；可以生成时返回空字符串。"""
    if spec.kind in ROAD_KINDS:
        # 编号已在 resolve 里严格校验，干线模板对 4 字符通用
        return ""

    if spec.kind not in EXPRESSWAY_KINDS:
        return f"未知标志类型：{spec.kind}"

    if expressway_key(spec) in EXPRESSWAY_TEMPLATES:
        return ""

    available = supported_name_lengths(spec.kind, spec.digit_count)
    if available:
        return (
            f"无对应模板：{spec.label} {spec.digit_count} 位编号目前只支持名称长度 "
            + " / ".join(available)
        )
    return f"无对应模板：{spec.label} 不支持 {spec.digit_count} 位编号"


def render_svg(spec: SignSpec) -> str:
    """生成 SVG 源码；无模板时抛 SignError。"""
    reason = validate(spec)
    if reason:
        raise SignError(reason)

    if spec.kind in ROAD_KINDS:
        return EXPRESSWAY_TEMPLATES[spec.kind].replace("ROAD_NUMBER", escape(spec.number))

    template = EXPRESSWAY_TEMPLATES[expressway_key(spec)]

    # 只有 4 位编号的模板把编号拆成两段，末位用小一号字
    if spec.digit_count == 4:
        part1, part2 = spec.number[:3], spec.number[3:]
    else:
        part1 = part2 = ""

    return _fill(template, {
        "HIGHWAY_NUMBER_PART1": part1,
        "HIGHWAY_NUMBER_PART2": part2,
        "HIGHWAY_NUMBER": spec.number,
        "HIGHWAY_NAME": spec.name,
        "PROVINCE": spec.province,
    })


def _fill(template: str, tokens: dict[str, str]) -> str:
    """单次替换全部占位符，按长度降序匹配，避免 HIGHWAY_NUMBER 抢先吃掉 PART1/PART2。"""
    pattern = re.compile("|".join(sorted(tokens, key=len, reverse=True)))
    return pattern.sub(lambda match: escape(tokens[match.group(0)]), template)


def diagnose(col1: str, col2: str, col3: str = "") -> tuple[bool, str]:
    """界面实时校验：(是否可生成, 说明)。"""
    spec, error = try_resolve(col1, col2, col3)
    if spec is None:
        return False, error
    reason = validate(spec)
    if reason:
        return False, reason
    return True, spec.title


# ---------- 文件命名 ----------

ROAD_ENGLISH = {
    SignKind.NATIONAL_ROAD: "China National Highway",
    SignKind.PROVINCIAL_ROAD: "China Provincial Highway",
    SignKind.COUNTY_ROAD: "China County Road",
    SignKind.TOWNSHIP_ROAD: "China Township Road",
}

_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def sanitize(name: str, fallback: str = "未命名") -> str:
    """去掉 Windows 文件名非法字符，避免写盘失败。"""
    cleaned = re.sub(r"\s+", " ", _ILLEGAL.sub("_", name)).strip().rstrip(". ")
    return cleaned or fallback


def build_basename(spec: SignSpec, style: str = "zh") -> str:
    """返回不含扩展名的文件名。"""
    if spec.kind in ROAD_ENGLISH:
        if style == "en":
            return sanitize(f"{ROAD_ENGLISH[spec.kind]} {spec.number}")
        return sanitize(f"{spec.label}_{spec.number}")

    has_name = bool(spec.name)
    if style == "en":
        if spec.kind == SignKind.NATIONAL_EXPRESSWAY:
            owner = "China"
        else:
            owner = PROVINCE_ENGLISH.get(spec.province, spec.province or "China")
        tail = "with name" if has_name else "no name"
        return sanitize(f"{owner} Expwy {spec.number} sign {tail}")

    if spec.kind == SignKind.NATIONAL_EXPRESSWAY:
        prefix = "国家高速"
    else:
        prefix = f"{spec.province}高速"
    return sanitize(f"{prefix}_{spec.number}_{spec.name or '无名称'}")
