from __future__ import annotations

import pandas as pd

from cleaner import apply_spec_resolution, clean_text, extract_product
from database import list_product_names


def scan_unique_skus(df: pd.DataFrame, item_column: str) -> pd.DataFrame:
    if item_column not in df.columns:
        raise ValueError(f"找不到选购商品字段：{item_column}")

    temp = pd.DataFrame({"原始规格": df[item_column].map(clean_text)})
    temp = temp[temp["原始规格"] != ""]
    grouped = (
        temp.groupby("原始规格", as_index=False)
        .size()
        .rename(columns={"size": "涉及订单数"})
    )

    product_names = list_product_names()
    grouped["产品"] = grouped["原始规格"].map(
        lambda value: extract_product(value, product_names)
    )

    results = [
        apply_spec_resolution(product, source_text)
        for product, source_text in zip(grouped["产品"], grouped["原始规格"])
    ]
    grouped["重量规格"] = [item[0] for item in results]
    grouped["果型"] = [item[1] for item in results]
    grouped["供应规格"] = [item[2] for item in results]
    grouped["识别来源"] = [item[3] for item in results]
    grouped["状态"] = grouped.apply(
        lambda row: (
            "已识别"
            if row["供应规格"]
            else ("产品未识别" if not row["产品"] else "待审核")
        ),
        axis=1,
    )

    return grouped.sort_values(
        by=["状态", "产品", "涉及订单数"],
        ascending=[True, True, False],
    ).reset_index(drop=True)
