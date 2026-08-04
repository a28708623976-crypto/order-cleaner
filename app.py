from __future__ import annotations

from io import BytesIO
from pathlib import Path
import re

import pandas as pd
import streamlit as st

from ai_service import AIConfigurationError, analyze_sku, get_model, is_ai_configured
from cleaner import clean_dataframe, resolve_fields
from config import DEFAULT_PRODUCTS
from database import (
    add_standard_spec,
    deactivate_product,
    delete_mapping_rule,
    delete_standard_spec,
    get_product_definition,
    initialize_database,
    list_mapping_rules,
    list_product_names,
    list_products,
    list_standard_specs,
    merge_product_definition,
    replace_product_definition,
    save_mapping_rule,
)
from excel_writer import dataframe_to_excel_bytes
from scanner import scan_unique_skus

st.set_page_config(page_title="智洗表 V2.3", page_icon="🧹", layout="wide")
initialize_database()

st.title("🧹 智洗表 V2.3")
st.caption("产品主数据 + 订单扫描审核 + 规则复用 + AI辅助")
if is_ai_configured():
    st.success(f"智谱AI已配置：{get_model()}｜HTTP直连模式")
else:
    st.warning("智谱AI尚未配置；固定规则、人工审核和导出仍可正常使用。")


def split_values(text: str) -> list[str]:
    return list(dict.fromkeys(
        item.strip() for item in re.split(r"[,，、;；\n]+", text or "") if item.strip()
    ))


tab_specs, tab_rules, tab_upload = st.tabs(["规格资料库", "映射规则库", "上传与清洗"])

with tab_specs:
    st.subheader("产品规格资料库")
    st.info("产品只维护一次；重量规格和果型可批量填写，系统自动生成组合。")

    records = list_products()
    names = [item["product"] for item in records]
    mode = st.radio("操作方式", ["新建产品", "维护已有产品"], horizontal=True)

    selected_product = ""
    current_weights: list[str] = []
    current_fruits: list[str] = []
    if mode == "维护已有产品" and names:
        selected_product = st.selectbox("选择产品", names)
        definition = get_product_definition(selected_product)
        current_weights = definition["weight_specs"]
        current_fruits = definition["fruit_types"]

    with st.form("product_master"):
        if mode == "新建产品":
            product_name = st.text_input("产品名称", placeholder="例如：榴莲蜜薯")
        else:
            product_name = selected_product
            st.text_input("产品名称", value=selected_product, disabled=True)

        c1, c2 = st.columns(2)
        weight_text = c1.text_area(
            "重量规格（可填写多个）",
            value="、".join(current_weights),
            placeholder="3斤、5斤、9斤",
            height=110,
        )
        fruit_text = c2.text_area(
            "果型（可填写多个）",
            value="、".join(current_fruits),
            placeholder="大果、中果、小果、圆果、拇指薯",
            height=110,
        )
        weights = split_values(weight_text)
        fruits = split_values(fruit_text)
        combinations = [f"{w}{f}" for w in weights for f in fruits]
        if combinations:
            st.caption(f"将生成 {len(combinations)} 个供应规格：" + "、".join(combinations))

        save_mode = st.radio("保存方式", ["合并补充", "按当前内容覆盖"], horizontal=True)
        if st.form_submit_button("保存产品资料", type="primary", use_container_width=True):
            try:
                if not product_name.strip():
                    raise ValueError("产品名称不能为空。")
                if save_mode == "合并补充":
                    count = merge_product_definition(product_name, weights, fruits)
                else:
                    count = replace_product_definition(product_name, weights, fruits)
                st.success(f"已保存“{product_name}”，当前共有 {count} 个供应规格。")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))

    st.divider()
    records = list_products()
    if records:
        st.dataframe(pd.DataFrame([
            {
                "产品": item["product"],
                "重量规格": "、".join(item["weight_specs"]) or "待补充",
                "果型": "、".join(item["fruit_types"]) or "待补充",
                "供应规格数量": item["spec_count"],
            }
            for item in records
        ]), hide_index=True, width="stretch")

        detail_name = st.selectbox("查看产品明细", [item["product"] for item in records])
        detail_specs = list_standard_specs(detail_name)
        if detail_specs:
            st.dataframe(pd.DataFrame([
                {
                    "ID": item["id"],
                    "重量规格": item["weight_spec"],
                    "果型": item["fruit_type"],
                    "供应规格": item["supplier_spec"],
                }
                for item in detail_specs
            ]), hide_index=True, width="stretch")
        else:
            st.warning("该产品尚未同时配置重量规格和果型。")

        with st.expander("删除资料"):
            delete_scope = st.radio("删除范围", ["单个供应规格", "整个产品"], horizontal=True)
            if delete_scope == "单个供应规格":
                all_specs = list_standard_specs()
                options = {
                    f'{item["product"]} / {item["supplier_spec"]}': int(item["id"])
                    for item in all_specs
                }
                label = st.selectbox("选择供应规格", ["请选择"] + list(options))
                if st.button("删除选中供应规格", disabled=label == "请选择"):
                    delete_standard_spec(options[label])
                    st.rerun()
            else:
                product_to_delete = st.selectbox("选择产品", [item["product"] for item in records])
                st.warning("删除整个产品后，相关标准规格和映射规则都会停用。")
                if st.button("删除整个产品"):
                    deactivate_product(product_to_delete)
                    st.rerun()
    else:
        st.info("暂无产品资料。")

with tab_rules:
    st.subheader("关键词映射规则库")
    rules = list_mapping_rules()
    if rules:
        rules_df = pd.DataFrame(rules)[
            ["id", "product", "source_text", "supplier_spec", "updated_at"]
        ]
        rules_df.columns = ["ID", "产品", "原始规格文本", "清洗结果", "更新时间"]
        st.dataframe(rules_df, hide_index=True, width="stretch")
        rule_options = {f'{r["product"]} / {r["supplier_spec"]} / ID {r["id"]}': int(r["id"]) for r in rules}
        rule_label = st.selectbox("删除规则", ["请选择"] + list(rule_options))
        if st.button("删除选中规则", disabled=rule_label == "请选择"):
            delete_mapping_rule(rule_options[rule_label])
            st.rerun()
    else:
        st.info("暂无映射规则。")

with tab_upload:
    uploaded = st.file_uploader("上传抖店订单文件", type=["xlsx", "csv"])
    if uploaded is not None:
        raw = uploaded.getvalue()
        suffix = Path(uploaded.name).suffix.lower()
        try:
            if suffix == ".xlsx":
                workbook = pd.ExcelFile(BytesIO(raw), engine="openpyxl")
                sheet = st.selectbox("选择工作表", workbook.sheet_names)
                source_df = pd.read_excel(BytesIO(raw), sheet_name=sheet, dtype=object, engine="openpyxl")
            else:
                encoding = st.selectbox("CSV编码", ["utf-8-sig", "utf-8", "gb18030"])
                source_df = pd.read_csv(BytesIO(raw), dtype=object, encoding=encoding)
        except Exception as exc:
            st.error(f"文件读取失败：{exc}")
            st.stop()

        source_df.columns = [str(col).strip() for col in source_df.columns]
        field_mapping = resolve_fields(list(source_df.columns))
        item_column = field_mapping.get("选购商品")
        if not item_column:
            st.error("未找到“选购商品”字段，请检查源文件表头。")
            st.stop()

        st.subheader("原始数据预览")
        c1, c2 = st.columns(2)
        c1.metric("数据行数", len(source_df))
        c2.metric("字段数量", len(source_df.columns))
        st.dataframe(source_df.head(15), hide_index=True, width="stretch")

        scan_df = scan_unique_skus(source_df, item_column)
        st.subheader("规格扫描结果")
        metrics = st.columns(4)
        metrics[0].metric("唯一规格数", len(scan_df))
        metrics[1].metric("已识别", int((scan_df["状态"] == "已识别").sum()))
        metrics[2].metric("待审核", int((scan_df["状态"] == "待审核").sum()))
        metrics[3].metric("产品未识别", int((scan_df["状态"] == "产品未识别").sum()))
        st.dataframe(scan_df, hide_index=True, width="stretch")

        st.subheader("人工审核")
        st.caption("下拉选择直接用于本次清洗；保存为复用规则仅影响下次是否自动识别。")
        unresolved = scan_df[scan_df["状态"] != "已识别"].copy()
        temporary_overrides: dict[str, dict[str, str]] = {}
        reusable_rules: list[tuple[str, str, int]] = []

        if unresolved.empty:
            st.success("所有唯一规格均已识别，可以直接生成结果。")
        else:
            for idx, row in unresolved.iterrows():
                title = f'{row["产品"] or "未识别产品"}｜{row["涉及订单数"]}行｜{row["原始规格"][:55]}'
                with st.expander(title, expanded=True):
                    st.code(row["原始规格"], language=None)
                    product_options = list_product_names() or DEFAULT_PRODUCTS
                    product_index = product_options.index(row["产品"]) if row["产品"] in product_options else 0
                    product = st.selectbox("产品", product_options, index=product_index, key=f"product_{idx}")

                    product_specs = list_standard_specs(product)
                    spec_by_name = {item["supplier_spec"]: item for item in product_specs}
                    available_specs = list(spec_by_name)
                    ai_key = f"ai_result_{idx}"
                    if st.button(
                        "AI分析规格",
                        key=f"ai_{idx}",
                        disabled=(not is_ai_configured()) or (not available_specs),
                    ):
                        try:
                            with st.spinner("智谱正在分析规格……"):
                                st.session_state[ai_key] = analyze_sku(
                                    source_text=row["原始规格"],
                                    detected_product=product,
                                    available_specs=available_specs,
                                )
                        except AIConfigurationError as exc:
                            st.error(str(exc))
                        except Exception as exc:
                            st.error(f"AI分析失败：{exc}")

                    ai_result = st.session_state.get(ai_key)
                    if ai_result:
                        recommendation = ai_result.get("recommended_spec", "")
                        confidence = float(ai_result.get("confidence", 0))
                        st.info(
                            f"AI建议：{recommendation or '无法判断'}｜置信度：{confidence:.0%}\n\n"
                            f'{ai_result.get("reason", "")}'
                        )
                        keywords = ai_result.get("suggested_keywords", [])
                        if keywords:
                            st.caption("建议复用关键词：" + "、".join(keywords))

                    options = ["请选择"] + available_specs + ["＋ 新增规格"]
                    recommendation = ai_result.get("recommended_spec", "") if ai_result else ""
                    default_index = options.index(recommendation) if recommendation in options else 0
                    selected = st.selectbox(
                        "本次清洗结果",
                        options,
                        index=default_index,
                        key=f"spec_{idx}",
                    )

                    if selected == "＋ 新增规格":
                        n1, n2 = st.columns(2)
                        new_weight = n1.text_input("新重量规格", key=f"weight_{idx}")
                        new_fruit = n2.text_input("新果型", key=f"fruit_{idx}")
                        save_new = st.checkbox("新增后同时保存为复用规则", value=True, key=f"save_new_{idx}")
                        if st.button("新增规格并应用", key=f"add_{idx}", type="primary"):
                            try:
                                spec_id = add_standard_spec(product, new_weight, new_fruit)
                                if save_new:
                                    save_mapping_rule(product, row["原始规格"], spec_id)
                                st.success("标准规格已新增，请重新选择该规格。")
                                st.rerun()
                            except Exception as exc:
                                st.error(str(exc))
                    elif selected != "请选择":
                        spec = spec_by_name[selected]
                        temporary_overrides[row["原始规格"]] = {
                            "product": product,
                            "weight_spec": spec["weight_spec"],
                            "fruit_type": spec["fruit_type"],
                            "supplier_spec": spec["supplier_spec"],
                        }
                        save_reuse = st.checkbox(
                            "同时保存为复用规则（下次自动识别）",
                            value=False,
                            key=f"reuse_{idx}",
                        )
                        if save_reuse:
                            reusable_rules.append((product, row["原始规格"], int(spec["id"])))
                        st.success(
                            f'本次将按“{spec["supplier_spec"]}”清洗'
                            + ("，并保存为复用规则。" if save_reuse else "，本次有效但不会保存。")
                        )

        unresolved_rows = int(
            unresolved[~unresolved["原始规格"].isin(temporary_overrides)]["涉及订单数"].sum()
        ) if not unresolved.empty else 0
        m1, m2, m3 = st.columns(3)
        m1.metric("本次人工选择规格数", len(temporary_overrides))
        m2.metric("准备保存复用规则", len(reusable_rules))
        m3.metric("仍可能留空订单行", unresolved_rows)
        if unresolved_rows:
            st.warning(f"还有约 {unresolved_rows} 行订单对应的规格尚未选择。")

        if st.button("应用本次选择并生成结果", type="primary", width="stretch"):
            try:
                for product, source_text, spec_id in reusable_rules:
                    save_mapping_rule(product, source_text, spec_id)
                cleaned_df, stats, _ = clean_dataframe(source_df, temporary_overrides)
                st.session_state["cleaned_df"] = cleaned_df
                st.session_state["clean_stats"] = stats.as_dict()
                st.session_state["excel_output"] = dataframe_to_excel_bytes(cleaned_df)
                st.session_state["excel_name"] = f"{Path(uploaded.name).stem}_抖店标准化清洗.xlsx"
                st.session_state["override_count"] = len(temporary_overrides)
                st.session_state["saved_rule_count"] = len(reusable_rules)
            except Exception as exc:
                st.error(f"生成结果失败：{exc}")

        if "cleaned_df" in st.session_state:
            st.subheader("最终结果预览")
            st.caption(
                f'本次应用 {st.session_state.get("override_count", 0)} 条人工选择；'
                f'其中 {st.session_state.get("saved_rule_count", 0)} 条已保存为复用规则。'
            )
            stats = st.session_state["clean_stats"]
            result_metrics = st.columns(5)
            result_metrics[0].metric("原始行数", stats["原始行数"])
            result_metrics[1].metric("输出行数", stats["输出行数"])
            result_metrics[2].metric("产品留空", stats["产品留空"])
            result_metrics[3].metric("果型留空", stats["果型留空"])
            result_metrics[4].metric("供应规格留空", stats["供应规格留空"])
            st.dataframe(st.session_state["cleaned_df"].head(50), hide_index=True, width="stretch")
            st.download_button(
                "下载标准化Excel",
                data=st.session_state["excel_output"],
                file_name=st.session_state["excel_name"],
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                type="primary",
                width="stretch",
            )
