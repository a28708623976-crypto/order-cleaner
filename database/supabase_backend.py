from __future__ import annotations

import atexit
import copy
import json
import os
import threading
import time
from typing import Any, Callable
from urllib.parse import quote

import httpx

from config import DEFAULT_PRODUCT_MASTER

DEFAULT_SEED_KEY = "confirmed_product_master_v1"
_INITIALIZED = False
_INITIALIZING = False

_CACHE_TTL_SECONDS = int(os.getenv("SUPABASE_CACHE_TTL", "120"))
_READ_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_LOCK = threading.RLock()
_CLIENT_LOCK = threading.RLock()
_HTTP_CLIENT: httpx.Client | None = None
_HTTP_CLIENT_SETTINGS: tuple[str, str] | None = None
_PRODUCT_SNAPSHOT: list[dict] | None = None
_MAPPING_RULE_INDEX: dict[tuple[str, str], dict] | None = None


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


def _get_client() -> httpx.Client:
    global _HTTP_CLIENT, _HTTP_CLIENT_SETTINGS
    settings = _settings()
    with _CLIENT_LOCK:
        if _HTTP_CLIENT is None or _HTTP_CLIENT_SETTINGS != settings:
            if _HTTP_CLIENT is not None:
                _HTTP_CLIENT.close()
            _HTTP_CLIENT = httpx.Client(
                timeout=httpx.Timeout(30.0, connect=10.0),
                limits=httpx.Limits(
                    max_connections=20,
                    max_keepalive_connections=10,
                    keepalive_expiry=60.0,
                ),
                transport=httpx.HTTPTransport(retries=2),
            )
            _HTTP_CLIENT_SETTINGS = settings
        return _HTTP_CLIENT


def _close_client() -> None:
    global _HTTP_CLIENT
    with _CLIENT_LOCK:
        if _HTTP_CLIENT is not None:
            _HTTP_CLIENT.close()
            _HTTP_CLIENT = None


atexit.register(_close_client)


def _cache_key(
    table: str,
    select: str,
    filters: dict[str, str] | None,
    order: str | None,
    limit: int | None,
) -> str:
    payload = {
        "table": table,
        "select": select,
        "filters": filters or {},
        "order": order,
        "limit": limit,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _cache_get(key: str) -> Any | None:
    now = time.monotonic()
    with _CACHE_LOCK:
        item = _READ_CACHE.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at <= now:
            _READ_CACHE.pop(key, None)
            return None
        return copy.deepcopy(value)


def _cache_set(key: str, value: Any) -> None:
    with _CACHE_LOCK:
        _READ_CACHE[key] = (
            time.monotonic() + _CACHE_TTL_SECONDS,
            copy.deepcopy(value),
        )


def _invalidate_cache() -> None:
    global _PRODUCT_SNAPSHOT, _MAPPING_RULE_INDEX
    with _CACHE_LOCK:
        _READ_CACHE.clear()
        _PRODUCT_SNAPSHOT = None
        _MAPPING_RULE_INDEX = None


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
        response = _get_client().request(
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

    if method.upper() != "GET":
        _invalidate_cache()

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
    use_cache: bool = True,
) -> list[dict]:
    key = _cache_key(table, select, filters, order, limit)
    if use_cache:
        cached = _cache_get(key)
        if cached is not None:
            return cached

    params: dict[str, str] = {"select": select}
    if filters:
        params.update(filters)
    if order:
        params["order"] = order
    if limit is not None:
        params["limit"] = str(limit)
    result = _request("GET", table, params=params) or []

    if use_cache:
        _cache_set(key, result)
    return result


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


def _upsert_many(
    table: str,
    payload: list[dict],
    *,
    on_conflict: str,
) -> list[dict]:
    if not payload:
        return []
    return _upsert(table, payload, on_conflict=on_conflict)


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


def _upsert_weights_internal(product_id: int, weights: list[str]) -> None:
    payload = [
        {
            "product_id": product_id,
            "weight_spec": weight,
            "is_active": True,
        }
        for weight in dict.fromkeys(weight.strip() for weight in weights if weight.strip())
    ]
    _upsert_many(
        "product_weights",
        payload,
        on_conflict="product_id,weight_spec",
    )


def _upsert_fruit_types_internal(product_id: int, fruits: list[str]) -> None:
    payload = [
        {
            "product_id": product_id,
            "fruit_type": fruit,
            "is_active": True,
        }
        for fruit in dict.fromkeys(fruit.strip() for fruit in fruits if fruit.strip())
    ]
    _upsert_many(
        "product_fruit_types",
        payload,
        on_conflict="product_id,fruit_type",
    )


def _upsert_standard_specs_internal(
    product: str,
    pairs: list[tuple[str, str]],
) -> list[dict]:
    payload = [
        {
            "product": product,
            "weight_spec": weight,
            "fruit_type": fruit,
            "supplier_spec": f"{weight}{fruit}",
            "is_active": True,
        }
        for weight, fruit in dict.fromkeys(pairs)
    ]
    return _upsert_many(
        "standard_specs",
        payload,
        on_conflict="product,weight_spec,fruit_type",
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


def _list_products_internal() -> list[dict]:
    global _PRODUCT_SNAPSHOT
    if _PRODUCT_SNAPSHOT is not None:
        return copy.deepcopy(_PRODUCT_SNAPSHOT)

    products = _select(
        "products",
        select="id,name",
        filters={"is_active": _eq("true")},
        order="name.asc",
    )
    if not products:
        _PRODUCT_SNAPSHOT = []
        return []

    product_ids = [int(row["id"]) for row in products]
    id_filter = _in(product_ids)
    weights = _select(
        "product_weights",
        select="product_id,weight_spec",
        filters={
            "product_id": id_filter,
            "is_active": _eq("true"),
        },
        order="product_id.asc,weight_spec.asc",
    )
    fruits = _select(
        "product_fruit_types",
        select="product_id,fruit_type",
        filters={
            "product_id": id_filter,
            "is_active": _eq("true"),
        },
        order="product_id.asc,fruit_type.asc",
    )

    weight_map: dict[int, list[str]] = {product_id: [] for product_id in product_ids}
    fruit_map: dict[int, list[str]] = {product_id: [] for product_id in product_ids}
    for row in weights:
        weight_map.setdefault(int(row["product_id"]), []).append(str(row["weight_spec"]))
    for row in fruits:
        fruit_map.setdefault(int(row["product_id"]), []).append(str(row["fruit_type"]))

    result: list[dict] = []
    for row in products:
        product_id = int(row["id"])
        weight_specs = weight_map.get(product_id, [])
        fruit_types = fruit_map.get(product_id, [])
        result.append({
            "id": product_id,
            "product": str(row["name"]),
            "weight_specs": weight_specs,
            "fruit_types": fruit_types,
            "spec_count": len(weight_specs) * len(fruit_types),
        })

    _PRODUCT_SNAPSHOT = copy.deepcopy(result)
    return result


def _list_product_names_internal() -> list[str]:
    return [item["product"] for item in _list_products_internal()]


def _get_product_definition_internal(product: str) -> dict:
    for item in _list_products_internal():
        if item["product"] == product:
            return item
    return {
        "product": product,
        "weight_specs": [],
        "fruit_types": [],
        "spec_count": 0,
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
    _upsert_weights_internal(product_id, weights)
    _upsert_fruit_types_internal(product_id, fruits)

    current = _get_product_definition_internal(product)
    pairs = [
        (weight, fruit)
        for weight in current["weight_specs"]
        for fruit in current["fruit_types"]
    ]
    _upsert_standard_specs_internal(product, pairs)
    return len(pairs)


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
    return _list_products_internal()


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
    _upsert_weights_internal(product_id, weights)
    _upsert_fruit_types_internal(product_id, fruits)

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

    _upsert_standard_specs_internal(product, list(active_pairs))
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
    global _MAPPING_RULE_INDEX
    initialize_database()
    if _MAPPING_RULE_INDEX is None:
        _MAPPING_RULE_INDEX = {
            (str(rule["product"]), str(rule["source_text"])): rule
            for rule in list_mapping_rules(active_only=True)
        }
    rule = _MAPPING_RULE_INDEX.get((product, source_text))
    return copy.deepcopy(rule) if rule else None


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
