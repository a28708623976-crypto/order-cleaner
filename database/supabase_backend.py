from __future__ import annotations

import os
from typing import Any

import httpx

from config import DEFAULT_PRODUCT_MASTER

DEFAULT_SEED_KEY = "confirmed_product_master_v1"
_INITIALIZED = False
_INITIALIZING = False


class SupabaseConfigurationError(RuntimeError):
    pass


class SupabaseRequestError(RuntimeError):
    pass


def _get_secret(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value:
        return value

    try:
        import streamlit as st

        return str(st.secrets.get(name, "")).strip()
    except Exception:
        return ""


def _settings() -> tuple[str, str]:
    url = _get_secret("SUPABASE_URL").rstrip("/")
    key = (
        _get_secret("SUPABASE_SECRET_KEY")
        or _get_secret("SUPABASE_SERVICE_ROLE_KEY")
    )
    if not url or not key:
        raise SupabaseConfigurationError(
            "尚未配置 SUPABASE_URL 和 SUPABASE_SECRET_KEY。"
        )
    return url, key


def _headers(prefer: str | None = None) -> dict[str, str]:
    _, key = _settings()
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def _request(
    method: str,
    table: str,
    *,
    params: dict[str, str] | None = None,
    json: Any = None,
    prefer: str | None = None,
) -> Any:
    url, _ = _settings()
    endpoint = f"{url}/rest/v1/{table}"

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.request(
                method,
                endpoint,
                params=params,
                json=json,
                headers=_headers(prefer),
            )
    except httpx.TimeoutException as exc:
        raise SupabaseRequestError("Supabase 请求超时，请稍后重试。") from exc
    except httpx.HTTPError as exc:
        raise SupabaseRequestError(f"Supabase 网络请求失败：{exc}") from exc

    if response.status_code >= 400:
        try:
            detail = response.json()
        except Exception:
            detail = response.text[:500]
        raise SupabaseRequestError(
            f"Supabase 返回错误（HTTP {response.status_code}）：{detail}"
        )

    if response.status_code == 204 or not response.content:
        return None
    return response.json()


def _select(
    table: str,
    *,
    select: str = "*",
    filters: dict[str, str] | None = None,
    order: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    params: dict[str, str] = {"select": select}
    if filters:
        params.update(filters)
    if order:
        params["order"] = order
    if limit is not None:
        params["limit"] = str(limit)
    result = _request("GET", table, params=params)
    return result or []


def _upsert(
    table: str,
    payload: dict | list[dict],
    *,
    on_conflict: str,
) -> list[dict]:
    params = {"on_conflict": on_conflict}
    result = _request(
        "POST",
        table,
        params=params,
        json=payload,
        prefer="resolution=merge-duplicates,return=representation",
    )
    return result or []


def _patch(
    table: str,
    payload: dict,
    *,
    filters: dict[str, str],
    return_rows: bool = False,
) -> list[dict]:
    result = _request(
        "PATCH",
        table,
        params=filters,
        json=payload,
        prefer=(
            "return=representation"
            if return_rows
            else "return=minimal"
        ),
    )
    return result or []


def _eq(value: Any) -> str:
    return f"eq.{value}"


def _in(values: list[int]) -> str:
    return "in.(" + ",".join(str(int(value)) for value in values) + ")"


def initialize_database() -> None:
    global _INITIALIZED, _INITIALIZING
    if _INITIALIZED or _INITIALIZING:
        return

    _INITIALIZING = True
    try:
        _select("app_meta", limit=1)
        _seed_confirmed_product_master_once_internal()
        _INITIALIZED = True
    finally:
        _INITIALIZING = False


def _get_or_create_product_internal(product: str) -> int:
    product = product.strip()
    if not product:
        raise ValueError("产品名称不能为空。")

    rows = _upsert(
        "products",
        {"name": product, "is_active": True},
        on_conflict="name",
    )
    if rows:
        return int(rows[0]["id"])

    existing = _select(
        "products",
        filters={"name": _eq(product)},
        limit=1,
    )
    if not existing:
        raise SupabaseRequestError("产品保存失败。")
    return int(existing[0]["id"])


def _upsert_weight_internal(product_id: int, weight_spec: str) -> None:
    weight_spec = weight_spec.strip()
    if not weight_spec:
        return
    _upsert(
        "product_weights",
        {
            "product_id": product_id,
            "weight_spec": weight_spec,
            "is_active": True,
        },
        on_conflict="product_id,weight_spec",
    )


def _upsert_fruit_type_internal(product_id: int, fruit_type: str) -> None:
    fruit_type = fruit_type.strip()
    if not fruit_type:
        return
    _upsert(
        "product_fruit_types",
        {
            "product_id": product_id,
            "fruit_type": fruit_type,
            "is_active": True,
        },
        on_conflict="product_id,fruit_type",
    )


def _upsert_standard_spec_internal(
    product: str,
    weight_spec: str,
    fruit_type: str,
) -> int:
    rows = _upsert(
        "standard_specs",
        {
            "product": product,
            "weight_spec": weight_spec,
            "fruit_type": fruit_type,
            "supplier_spec": f"{weight_spec}{fruit_type}",
            "is_active": True,
        },
        on_conflict="product,weight_spec,fruit_type",
    )
    if rows:
        return int(rows[0]["id"])

    existing = _select(
        "standard_specs",
        filters={
            "product": _eq(product),
            "weight_spec": _eq(weight_spec),
            "fruit_type": _eq(fruit_type),
        },
        limit=1,
    )
    if not existing:
        raise SupabaseRequestError("标准规格保存失败。")
    return int(existing[0]["id"])


def _list_product_names_internal() -> list[str]:
    rows = _select(
        "products",
        select="name",
        filters={"is_active": _eq("true")},
        order="name.asc",
    )
    return [str(row["name"]) for row in rows]


def _get_product_definition_internal(product: str) -> dict:
    products = _select(
        "products",
        select="id,name",
        filters={
            "name": _eq(product),
            "is_active": _eq("true"),
        },
        limit=1,
    )
    if not products:
        return {
            "product": product,
            "weight_specs": [],
            "fruit_types": [],
            "spec_count": 0,
        }

    product_id = int(products[0]["id"])
    weights = _select(
        "product_weights",
        select="weight_spec",
        filters={
            "product_id": _eq(product_id),
            "is_active": _eq("true"),
        },
        order="weight_spec.asc",
    )
    fruits = _select(
        "product_fruit_types",
        select="fruit_type",
        filters={
            "product_id": _eq(product_id),
            "is_active": _eq("true"),
        },
        order="fruit_type.asc",
    )

    weight_specs = [str(row["weight_spec"]) for row in weights]
    fruit_types = [str(row["fruit_type"]) for row in fruits]
    return {
        "product": product,
        "weight_specs": weight_specs,
        "fruit_types": fruit_types,
        "spec_count": len(weight_specs) * len(fruit_types),
    }


def _list_standard_specs_internal(
    product: str | None = None,
    active_only: bool = True,
) -> list[dict]:
    filters: dict[str, str] = {}
    if product:
        filters["product"] = _eq(product)
    if active_only:
        filters["is_active"] = _eq("true")

    return _select(
        "standard_specs",
        filters=filters,
        order="product.asc,weight_spec.asc,fruit_type.asc",
    )


def _merge_product_definition_internal(
    product: str,
    weight_specs: list[str],
    fruit_types: list[str],
) -> int:
    product = product.strip()
    weights = list(dict.fromkeys(
        item.strip() for item in weight_specs if item and item.strip()
    ))
    fruits = list(dict.fromkeys(
        item.strip() for item in fruit_types if item and item.strip()
    ))

    product_id = _get_or_create_product_internal(product)
    for weight in weights:
        _upsert_weight_internal(product_id, weight)
    for fruit in fruits:
        _upsert_fruit_type_internal(product_id, fruit)

    current = _get_product_definition_internal(product)
    for weight in current["weight_specs"]:
        for fruit in current["fruit_types"]:
            _upsert_standard_spec_internal(product, weight, fruit)

    return len(_list_standard_specs_internal(product))


def _seed_confirmed_product_master_once_internal() -> None:
    rows = _select(
        "app_meta",
        filters={"key": _eq(DEFAULT_SEED_KEY)},
        limit=1,
    )
    if rows:
        return

    _upsert(
        "app_meta",
        {"key": DEFAULT_SEED_KEY, "value": "seeding"},
        on_conflict="key",
    )
    for product, definition in DEFAULT_PRODUCT_MASTER.items():
        _merge_product_definition_internal(
            product,
            definition.get("weights", []),
            definition.get("fruit_types", []),
        )
    _upsert(
        "app_meta",
        {"key": DEFAULT_SEED_KEY, "value": "1"},
        on_conflict="key",
    )


def list_product_names() -> list[str]:
    initialize_database()
    return _list_product_names_internal()


def list_products() -> list[dict]:
    initialize_database()
    return [
        _get_product_definition_internal(product)
        for product in _list_product_names_internal()
    ]


def get_product_definition(product: str) -> dict:
    initialize_database()
    return _get_product_definition_internal(product)


def merge_product_definition(
    product: str,
    weight_specs: list[str],
    fruit_types: list[str],
) -> int:
    initialize_database()
    return _merge_product_definition_internal(
        product,
        weight_specs,
        fruit_types,
    )


def replace_product_definition(
    product: str,
    weight_specs: list[str],
    fruit_types: list[str],
) -> int:
    initialize_database()

    product = product.strip()
    weights = list(dict.fromkeys(
        item.strip() for item in weight_specs if item and item.strip()
    ))
    fruits = list(dict.fromkeys(
        item.strip() for item in fruit_types if item and item.strip()
    ))
    product_id = _get_or_create_product_internal(product)

    _patch(
        "product_weights",
        {"is_active": False},
        filters={"product_id": _eq(product_id)},
    )
    _patch(
        "product_fruit_types",
        {"is_active": False},
        filters={"product_id": _eq(product_id)},
    )
    for weight in weights:
        _upsert_weight_internal(product_id, weight)
    for fruit in fruits:
        _upsert_fruit_type_internal(product_id, fruit)

    active_pairs = {(weight, fruit) for weight in weights for fruit in fruits}
    existing_specs = _select(
        "standard_specs",
        select="id,weight_spec,fruit_type",
        filters={"product": _eq(product)},
    )
    removed_ids = [
        int(row["id"])
        for row in existing_specs
        if (str(row["weight_spec"]), str(row["fruit_type"]))
        not in active_pairs
    ]

    _patch(
        "standard_specs",
        {"is_active": False},
        filters={"product": _eq(product)},
    )
    if removed_ids:
        _patch(
            "mapping_rules",
            {"is_active": False},
            filters={"standard_spec_id": _in(removed_ids)},
        )

    for weight, fruit in active_pairs:
        _upsert_standard_spec_internal(product, weight, fruit)
    return len(active_pairs)


def add_standard_spec(
    product: str,
    weight_spec: str,
    fruit_type: str,
) -> int:
    initialize_database()
    product = product.strip()
    weight_spec = weight_spec.strip()
    fruit_type = fruit_type.strip()
    if not all([product, weight_spec, fruit_type]):
        raise ValueError("产品、重量规格、果型均不能为空。")

    product_id = _get_or_create_product_internal(product)
    _upsert_weight_internal(product_id, weight_spec)
    _upsert_fruit_type_internal(product_id, fruit_type)
    return _upsert_standard_spec_internal(product, weight_spec, fruit_type)


def list_standard_specs(
    product: str | None = None,
    active_only: bool = True,
) -> list[dict]:
    initialize_database()
    return _list_standard_specs_internal(product, active_only)


def get_standard_spec(spec_id: int) -> dict | None:
    initialize_database()
    rows = _select(
        "standard_specs",
        filters={"id": _eq(spec_id)},
        limit=1,
    )
    return rows[0] if rows else None


def delete_standard_spec(spec_id: int) -> None:
    initialize_database()
    _patch(
        "standard_specs",
        {"is_active": False},
        filters={"id": _eq(spec_id)},
    )
    _patch(
        "mapping_rules",
        {"is_active": False},
        filters={"standard_spec_id": _eq(spec_id)},
    )


def deactivate_product(product: str) -> None:
    initialize_database()
    rows = _select(
        "products",
        select="id",
        filters={"name": _eq(product)},
        limit=1,
    )
    if not rows:
        return

    product_id = int(rows[0]["id"])
    specs = _select(
        "standard_specs",
        select="id",
        filters={"product": _eq(product)},
    )
    spec_ids = [int(row["id"]) for row in specs]

    _patch("products", {"is_active": False}, filters={"id": _eq(product_id)})
    _patch(
        "product_weights",
        {"is_active": False},
        filters={"product_id": _eq(product_id)},
    )
    _patch(
        "product_fruit_types",
        {"is_active": False},
        filters={"product_id": _eq(product_id)},
    )
    _patch(
        "standard_specs",
        {"is_active": False},
        filters={"product": _eq(product)},
    )
    if spec_ids:
        _patch(
            "mapping_rules",
            {"is_active": False},
            filters={"standard_spec_id": _in(spec_ids)},
        )


def list_mapping_rules(active_only: bool = True) -> list[dict]:
    initialize_database()
    filters = {"is_active": _eq("true")} if active_only else {}
    rules = _select(
        "mapping_rules",
        filters=filters,
        order="product.asc,source_text.asc",
    )
    if not rules:
        return []

    spec_ids = sorted({int(rule["standard_spec_id"]) for rule in rules})
    specs = _select(
        "standard_specs",
        select="id,weight_spec,fruit_type,supplier_spec,is_active",
        filters={"id": _in(spec_ids)},
    )
    spec_map = {int(spec["id"]): spec for spec in specs}

    result: list[dict] = []
    for rule in rules:
        spec = spec_map.get(int(rule["standard_spec_id"]))
        if not spec:
            continue
        if active_only and not bool(spec.get("is_active")):
            continue
        result.append({**rule, **{
            "weight_spec": spec["weight_spec"],
            "fruit_type": spec["fruit_type"],
            "supplier_spec": spec["supplier_spec"],
        }})
    return result


def get_mapping_rule(product: str, source_text: str) -> dict | None:
    initialize_database()
    rows = _select(
        "mapping_rules",
        filters={
            "product": _eq(product),
            "source_text": _eq(source_text),
            "is_active": _eq("true"),
        },
        limit=1,
    )
    if not rows:
        return None

    rule = rows[0]
    spec = get_standard_spec(int(rule["standard_spec_id"]))
    if not spec or not bool(spec.get("is_active")):
        return None
    return {
        **rule,
        "weight_spec": spec["weight_spec"],
        "fruit_type": spec["fruit_type"],
        "supplier_spec": spec["supplier_spec"],
    }


def save_mapping_rule(
    product: str,
    source_text: str,
    standard_spec_id: int,
) -> int:
    initialize_database()
    product = product.strip()
    source_text = source_text.strip()
    if not product or not source_text:
        raise ValueError("产品和原始规格文本不能为空。")

    rows = _upsert(
        "mapping_rules",
        {
            "product": product,
            "source_text": source_text,
            "standard_spec_id": int(standard_spec_id),
            "is_active": True,
        },
        on_conflict="product,source_text",
    )
    if rows:
        return int(rows[0]["id"])

    existing = _select(
        "mapping_rules",
        select="id",
        filters={
            "product": _eq(product),
            "source_text": _eq(source_text),
        },
        limit=1,
    )
    if not existing:
        raise SupabaseRequestError("映射规则保存失败。")
    return int(existing[0]["id"])


def delete_mapping_rule(rule_id: int) -> None:
    initialize_database()
    _patch(
        "mapping_rules",
        {"is_active": False},
        filters={"id": _eq(rule_id)},
    )
