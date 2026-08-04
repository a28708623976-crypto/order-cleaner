from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

import pandas as pd

from config import COURIER_NAMES, DEFAULT_PRODUCTS, FIELD_ALIASES, FINAL_COLUMNS
from database import get_mapping_rule, list_product_names

CONTROL_CHARS_RE = re.compile(r"[\t\r\n\u200b\ufeff]")
WEIGHT_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*斤")
TRACKING_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z0-9]{8,})(?![A-Za-z0-9])")
BRACKET_RE = re.compile(r"【([^】]+)】")
COMMON_TRACKING_PREFIXES = ("YT", "SF", "JD", "JT", "ZTO", "STO", "YTO", "YD", "EMS", "DB", "ANE")


@dataclass
class CleaningStats:
    original_rows: int
    output_rows: int
    product_blank: int
    weight_blank: int
    fruit_type_blank: int
    supplier_spec_blank: int
    tracking_blank: int

    def as_dict(self) -> dict[str, int]:
        return {
            "原始行数": self.original_rows,
            "输出行数": self.output_rows,
            "产品留空": self.product_blank,
            "重量规格留空": self.weight_blank,
            "果型留空": self.fruit_type_blank,
            "供应规格留空": self.supplier_spec_blank,
            "快递单号留空": self.tracking_blank,
        }


def is_blank(value: Any) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except Exception:
        pass
    return str(value).strip() == ""


def clean_text(value: Any) -> str:
    if is_blank(value):
        return ""
    return CONTROL_CHARS_RE.sub("", str(value)).strip()


def resolve_fields(columns: list[str]) -> dict[str, str]:
    normalized = {clean_text(col): col for col in columns}
    mapping: dict[str, str] = {}
    for standard, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                mapping[standard] = normalized[alias]
                break
    return mapping


def series_for(df: pd.DataFrame, mapping: dict[str, str], standard: str) -> pd.Series:
    source = mapping.get(standard)
    if source is None:
        return pd.Series([""] * len(df), index=df.index, dtype="object")
    return df[source]


def clean_order_id(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    return text


def clean_date(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return ""
    return parsed.strftime("%Y-%m-%d")


def clean_quantity(value: Any) -> int | str:
    text = clean_text(value).replace(",", "")
    if not text:
        return ""
    try:
        number = float(text)
    except ValueError:
        return ""
    return int(number) if math.isfinite(number) and number.is_integer() else ""


def clean_amount(value: Any) -> float | int | str:
    text = clean_text(value).replace(",", "")
    text = re.sub(r"^[￥¥]\s*", "", text)
    if not text:
        return ""
    try:
        number = float(text)
    except ValueError:
        return ""
    if not math.isfinite(number):
        return ""
    return int(number) if number.is_integer() else number


def extract_product(
    value: Any,
    product_names: list[str] | None = None,
) -> str:
    text = clean_text(value)
    if not text:
        return ""

    products = product_names or list_product_names() or DEFAULT_PRODUCTS
    products = sorted(set(products), key=len, reverse=True)

    bracket_candidates = []
    for bracket in BRACKET_RE.findall(text):
        bracket_candidates.extend(
            [product for product in products if product in bracket]
        )
    bracket_candidates = list(dict.fromkeys(bracket_candidates))

    if len(bracket_candidates) == 1:
        return bracket_candidates[0]
    if len(bracket_candidates) > 1:
        return ""

    candidates = list(dict.fromkeys(
        product for product in products if product in text
    ))
    return candidates[0] if len(candidates) == 1 else ""


def extract_weight(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    weights = {float(x) for x in WEIGHT_RE.findall(text)}
    if len(weights) != 1:
        return ""
    weight = next(iter(weights))
    if 1.8 <= weight < 3.5:
        return "3斤"
    if 3.5 <= weight < 7.5:
        return "5斤"
    if 7.5 <= weight <= 10.5:
        return "9斤"
    return ""


def extract_default_fruit_type(value: Any, product: str) -> str:
    text = clean_text(value)
    if not text or not product:
        return ""

    # 默认规则只处理明确且独立的规格词；复杂组合交给人工审核。
    candidates: list[tuple[int, str]] = []

    if product == "榴莲蜜薯":
        aliases = {
            "迷你薯": "拇指薯",
            "拇指薯": "拇指薯",
            "糖豆圆果": "圆果",
            "糖豆果": "圆果",
        }
        for token, target in aliases.items():
            for match in re.finditer(re.escape(token), text):
                candidates.append((match.start(), target))

    allowed = ["大果", "中果", "小果"]
    if product == "六鳌地瓜":
        allowed = ["中小果", "大果"]

    for token in sorted(allowed, key=len, reverse=True):
        for match in re.finditer(re.escape(token), text):
            start = match.start()
            if token == "大果" and start > 0 and text[start - 1] == "中":
                continue
            if token == "小果" and start > 0 and text[start - 1] == "中":
                continue
            candidates.append((start, token))

    unique_targets = list(dict.fromkeys(target for _, target in candidates))
    if len(unique_targets) == 1:
        return unique_targets[0]
    return ""


def merge_address(row: pd.Series, mapping: dict[str, str]) -> str:
    parts = []
    for field in ["省", "市", "区", "详细地址"]:
        source = mapping.get(field)
        if source:
            value = clean_text(row.get(source, ""))
            if value:
                parts.append(value)
    return "".join(parts)


def extract_logistics(info_value: Any, courier_value: Any = "") -> tuple[str, str]:
    info = clean_text(info_value)
    courier = clean_text(courier_value).strip(" -—–")
    tracking = ""

    leading = re.match(r"^\s*([A-Za-z0-9]{8,})(?=\s*[-—–,，;；|/])", info)
    if leading and any(ch.isdigit() for ch in leading.group(1)):
        tracking = leading.group(1)

    if not tracking:
        candidates = [
            token for token in TRACKING_RE.findall(info)
            if any(ch.isdigit() for ch in token)
        ]
        candidates = list(dict.fromkeys(candidates))
        preferred = [x for x in candidates if x.upper().startswith(COMMON_TRACKING_PREFIXES)]
        tracking = preferred[0] if preferred else (candidates[0] if candidates else "")

    if not courier and info:
        found = sorted({name for name in COURIER_NAMES if name in info}, key=len, reverse=True)
        if found:
            courier = found[0]

    return tracking, courier


def apply_spec_resolution(product: str, source_text: str) -> tuple[str, str, str, str]:
    """
    Returns: weight_spec, fruit_type, supplier_spec, resolution_source
    """
    rule = get_mapping_rule(product, source_text) if product else None
    if rule:
        return (
            rule["weight_spec"],
            rule["fruit_type"],
            rule["supplier_spec"],
            "历史规则",
        )

    weight = extract_weight(source_text)
    fruit = extract_default_fruit_type(source_text, product)
    supplier = f"{weight}{fruit}" if product and weight and fruit else ""
    source = "系统规则" if supplier else "待审核"
    return weight, fruit, supplier, source


def clean_dataframe(
    df: pd.DataFrame,
    temporary_overrides: dict[str, dict[str, str]] | None = None,
) -> tuple[pd.DataFrame, CleaningStats, dict[str, str]]:
    source = df.copy()
    source.columns = [clean_text(col) for col in source.columns]
    mapping = resolve_fields(list(source.columns))

    result = pd.DataFrame(index=source.index)
    result["主订单编号"] = series_for(source, mapping, "主订单编号").map(clean_order_id)
    result["子订单编号"] = series_for(source, mapping, "子订单编号").map(clean_order_id)
    result["选购商品"] = series_for(source, mapping, "选购商品").map(clean_text)
    overrides = temporary_overrides or {}
    product_names = list_product_names()

    detected_products = result["选购商品"].map(
        lambda value: extract_product(value, product_names)
    )

    # A one-time manual selection can also correct the detected product.
    result["产品"] = [
        overrides.get(clean_text(source_text), {}).get("product", detected_product)
        or detected_product
        for source_text, detected_product in zip(
            result["选购商品"],
            detected_products,
        )
    ]

    resolved = []
    for product, source_text in zip(result["产品"], result["选购商品"]):
        override = overrides.get(clean_text(source_text))
        if override:
            resolved.append(
                (
                    override.get("weight_spec", ""),
                    override.get("fruit_type", ""),
                    override.get("supplier_spec", ""),
                    "本次人工选择",
                )
            )
        else:
            resolved.append(apply_spec_resolution(product, source_text))

    result["重量规格"] = [x[0] for x in resolved]
    result["果型"] = [x[1] for x in resolved]
    result["供应规格"] = [x[2] for x in resolved]

    result["商品数量"] = series_for(source, mapping, "商品数量").map(clean_quantity)
    result["商品金额"] = series_for(source, mapping, "商品金额").map(clean_amount)

    for column in ["订单提交时间", "订单完成时间", "发货时间"]:
        result[column] = series_for(source, mapping, column).map(clean_date)

    result["订单状态"] = series_for(source, mapping, "订单状态").map(clean_text)
    result["售后状态"] = series_for(source, mapping, "售后状态").map(clean_text)
    result["收货地址"] = source.apply(lambda row: merge_address(row, mapping), axis=1)

    infos = series_for(source, mapping, "快递信息")
    couriers = series_for(source, mapping, "快递公司")
    logistics = [extract_logistics(i, c) for i, c in zip(infos, couriers)]
    result["快递信息 (单号)"] = [x[0] for x in logistics]
    result["快递公司"] = [x[1] for x in logistics]

    result = result.reindex(columns=FINAL_COLUMNS).fillna("")

    stats = CleaningStats(
        original_rows=len(source),
        output_rows=len(result),
        product_blank=int((result["产品"] == "").sum()),
        weight_blank=int((result["重量规格"] == "").sum()),
        fruit_type_blank=int((result["果型"] == "").sum()),
        supplier_spec_blank=int((result["供应规格"] == "").sum()),
        tracking_blank=int((result["快递信息 (单号)"] == "").sum()),
    )
    return result, stats, mapping
