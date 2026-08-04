from __future__ import annotations

import sqlite3
from pathlib import Path

from config import DEFAULT_PRODUCT_MASTER

DB_PATH = Path(__file__).with_name("ruleflow.db")
DEFAULT_SEED_KEY = "confirmed_product_master_v1"
_INITIALIZING_PATHS: set[str] = set()


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _create_schema() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS app_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS product_weights (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                weight_spec TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(product_id, weight_spec),
                FOREIGN KEY(product_id) REFERENCES products(id)
            );

            CREATE TABLE IF NOT EXISTS product_fruit_types (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                fruit_type TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(product_id, fruit_type),
                FOREIGN KEY(product_id) REFERENCES products(id)
            );

            CREATE TABLE IF NOT EXISTS standard_specs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product TEXT NOT NULL,
                weight_spec TEXT NOT NULL,
                fruit_type TEXT NOT NULL,
                supplier_spec TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(product, weight_spec, fruit_type)
            );

            CREATE TABLE IF NOT EXISTS mapping_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product TEXT NOT NULL,
                source_text TEXT NOT NULL,
                standard_spec_id INTEGER NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(product, source_text),
                FOREIGN KEY(standard_spec_id) REFERENCES standard_specs(id)
            );
            """
        )


def initialize_database() -> None:
    """
    Initialize the schema, migrate V2.0 data, and seed confirmed defaults once.
    The guard prevents recursive initialization while defaults are being seeded.
    """
    key = str(DB_PATH.absolute())
    if key in _INITIALIZING_PATHS:
        return

    _INITIALIZING_PATHS.add(key)
    try:
        _create_schema()
        _migrate_existing_specs_to_product_master_internal()
        _seed_confirmed_product_master_once_internal()
    finally:
        _INITIALIZING_PATHS.discard(key)


def _get_or_create_product_internal(product: str) -> int:
    product = product.strip()
    if not product:
        raise ValueError("产品名称不能为空。")

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO products(name, is_active)
            VALUES (?, 1)
            ON CONFLICT(name)
            DO UPDATE SET is_active = 1, updated_at = CURRENT_TIMESTAMP
            """,
            (product,),
        )
        row = conn.execute(
            "SELECT id FROM products WHERE name = ?",
            (product,),
        ).fetchone()
        return int(row["id"])


def _upsert_weight_internal(product_id: int, weight_spec: str) -> None:
    weight_spec = weight_spec.strip()
    if not weight_spec:
        return

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO product_weights(product_id, weight_spec, is_active)
            VALUES (?, ?, 1)
            ON CONFLICT(product_id, weight_spec)
            DO UPDATE SET is_active = 1, updated_at = CURRENT_TIMESTAMP
            """,
            (product_id, weight_spec),
        )


def _upsert_fruit_type_internal(product_id: int, fruit_type: str) -> None:
    fruit_type = fruit_type.strip()
    if not fruit_type:
        return

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO product_fruit_types(product_id, fruit_type, is_active)
            VALUES (?, ?, 1)
            ON CONFLICT(product_id, fruit_type)
            DO UPDATE SET is_active = 1, updated_at = CURRENT_TIMESTAMP
            """,
            (product_id, fruit_type),
        )


def _upsert_standard_spec_internal(
    product: str,
    weight_spec: str,
    fruit_type: str,
) -> int:
    supplier_spec = f"{weight_spec}{fruit_type}"

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO standard_specs(
                product, weight_spec, fruit_type, supplier_spec, is_active
            )
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(product, weight_spec, fruit_type)
            DO UPDATE SET
                supplier_spec = excluded.supplier_spec,
                is_active = 1,
                updated_at = CURRENT_TIMESTAMP
            """,
            (product, weight_spec, fruit_type, supplier_spec),
        )
        row = conn.execute(
            """
            SELECT id
            FROM standard_specs
            WHERE product = ? AND weight_spec = ? AND fruit_type = ?
            """,
            (product, weight_spec, fruit_type),
        ).fetchone()
        return int(row["id"])


def _list_product_names_internal() -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT name
            FROM products
            WHERE is_active = 1
            ORDER BY name
            """
        ).fetchall()
    return [str(row["name"]) for row in rows]


def _get_product_definition_internal(product: str) -> dict:
    with connect() as conn:
        product_row = conn.execute(
            """
            SELECT id, name
            FROM products
            WHERE name = ? AND is_active = 1
            """,
            (product,),
        ).fetchone()

        if not product_row:
            return {
                "product": product,
                "weight_specs": [],
                "fruit_types": [],
                "spec_count": 0,
            }

        product_id = int(product_row["id"])
        weights = [
            str(row["weight_spec"])
            for row in conn.execute(
                """
                SELECT weight_spec
                FROM product_weights
                WHERE product_id = ? AND is_active = 1
                ORDER BY weight_spec
                """,
                (product_id,),
            ).fetchall()
        ]
        fruit_types = [
            str(row["fruit_type"])
            for row in conn.execute(
                """
                SELECT fruit_type
                FROM product_fruit_types
                WHERE product_id = ? AND is_active = 1
                ORDER BY fruit_type
                """,
                (product_id,),
            ).fetchall()
        ]

    return {
        "product": product,
        "weight_specs": weights,
        "fruit_types": fruit_types,
        "spec_count": len(weights) * len(fruit_types),
    }


def _list_standard_specs_internal(
    product: str | None = None,
    active_only: bool = True,
) -> list[dict]:
    sql = "SELECT * FROM standard_specs WHERE 1=1"
    params: list[object] = []

    if product:
        sql += " AND product = ?"
        params.append(product)
    if active_only:
        sql += " AND is_active = 1"

    sql += " ORDER BY product, weight_spec, fruit_type"

    with connect() as conn:
        return [
            dict(row)
            for row in conn.execute(sql, params).fetchall()
        ]


def _merge_product_definition_internal(
    product: str,
    weight_specs: list[str],
    fruit_types: list[str],
) -> int:
    product = product.strip()
    weights = list(dict.fromkeys(
        item.strip()
        for item in weight_specs
        if item and item.strip()
    ))
    fruits = list(dict.fromkeys(
        item.strip()
        for item in fruit_types
        if item and item.strip()
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


def _migrate_existing_specs_to_product_master_internal() -> None:
    """Backfill product master tables from any existing V2.0 standard specs."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT product, weight_spec, fruit_type
            FROM standard_specs
            WHERE is_active = 1
            """
        ).fetchall()

    for row in rows:
        product_id = _get_or_create_product_internal(str(row["product"]))
        _upsert_weight_internal(product_id, str(row["weight_spec"]))
        _upsert_fruit_type_internal(product_id, str(row["fruit_type"]))


def _seed_confirmed_product_master_once_internal() -> None:
    with connect() as conn:
        seeded = conn.execute(
            "SELECT value FROM app_meta WHERE key = ?",
            (DEFAULT_SEED_KEY,),
        ).fetchone()

    if seeded:
        return

    # Mark first to prevent re-entry even if future code calls a public helper.
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO app_meta(key, value) VALUES (?, ?)",
            (DEFAULT_SEED_KEY, "seeding"),
        )

    for product, definition in DEFAULT_PRODUCT_MASTER.items():
        _merge_product_definition_internal(
            product=product,
            weight_specs=definition.get("weights", []),
            fruit_types=definition.get("fruit_types", []),
        )

    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO app_meta(key, value) VALUES (?, ?)",
            (DEFAULT_SEED_KEY, "1"),
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
        item.strip()
        for item in weight_specs
        if item and item.strip()
    ))
    fruits = list(dict.fromkeys(
        item.strip()
        for item in fruit_types
        if item and item.strip()
    ))

    product_id = _get_or_create_product_internal(product)

    with connect() as conn:
        conn.execute(
            "UPDATE product_weights SET is_active = 0 WHERE product_id = ?",
            (product_id,),
        )
        conn.execute(
            "UPDATE product_fruit_types SET is_active = 0 WHERE product_id = ?",
            (product_id,),
        )

    for weight in weights:
        _upsert_weight_internal(product_id, weight)
    for fruit in fruits:
        _upsert_fruit_type_internal(product_id, fruit)

    active_pairs = {
        (weight, fruit)
        for weight in weights
        for fruit in fruits
    }

    with connect() as conn:
        existing_specs = conn.execute(
            """
            SELECT id, weight_spec, fruit_type
            FROM standard_specs
            WHERE product = ?
            """,
            (product,),
        ).fetchall()

        removed_ids = [
            int(row["id"])
            for row in existing_specs
            if (str(row["weight_spec"]), str(row["fruit_type"]))
            not in active_pairs
        ]

        conn.execute(
            "UPDATE standard_specs SET is_active = 0 WHERE product = ?",
            (product,),
        )

        if removed_ids:
            placeholders = ",".join("?" for _ in removed_ids)
            conn.execute(
                f"""
                UPDATE mapping_rules
                SET is_active = 0
                WHERE standard_spec_id IN ({placeholders})
                """,
                removed_ids,
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

    return _upsert_standard_spec_internal(
        product,
        weight_spec,
        fruit_type,
    )


def list_standard_specs(
    product: str | None = None,
    active_only: bool = True,
) -> list[dict]:
    initialize_database()
    return _list_standard_specs_internal(product, active_only)


def get_standard_spec(spec_id: int) -> dict | None:
    initialize_database()
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM standard_specs WHERE id = ?",
            (spec_id,),
        ).fetchone()
        return dict(row) if row else None


def delete_standard_spec(spec_id: int) -> None:
    initialize_database()
    with connect() as conn:
        conn.execute(
            "UPDATE standard_specs SET is_active = 0 WHERE id = ?",
            (spec_id,),
        )
        conn.execute(
            """
            UPDATE mapping_rules
            SET is_active = 0
            WHERE standard_spec_id = ?
            """,
            (spec_id,),
        )


def deactivate_product(product: str) -> None:
    initialize_database()

    with connect() as conn:
        product_row = conn.execute(
            "SELECT id FROM products WHERE name = ?",
            (product,),
        ).fetchone()

        if not product_row:
            return

        product_id = int(product_row["id"])
        spec_ids = [
            int(row["id"])
            for row in conn.execute(
                "SELECT id FROM standard_specs WHERE product = ?",
                (product,),
            ).fetchall()
        ]

        conn.execute(
            "UPDATE products SET is_active = 0 WHERE id = ?",
            (product_id,),
        )
        conn.execute(
            "UPDATE product_weights SET is_active = 0 WHERE product_id = ?",
            (product_id,),
        )
        conn.execute(
            "UPDATE product_fruit_types SET is_active = 0 WHERE product_id = ?",
            (product_id,),
        )
        conn.execute(
            "UPDATE standard_specs SET is_active = 0 WHERE product = ?",
            (product,),
        )

        if spec_ids:
            placeholders = ",".join("?" for _ in spec_ids)
            conn.execute(
                f"""
                UPDATE mapping_rules
                SET is_active = 0
                WHERE standard_spec_id IN ({placeholders})
                """,
                spec_ids,
            )


def list_mapping_rules(active_only: bool = True) -> list[dict]:
    initialize_database()

    sql = """
        SELECT mr.*, ss.weight_spec, ss.fruit_type, ss.supplier_spec
        FROM mapping_rules mr
        JOIN standard_specs ss ON ss.id = mr.standard_spec_id
        WHERE 1=1
    """

    if active_only:
        sql += " AND mr.is_active = 1 AND ss.is_active = 1"

    sql += " ORDER BY mr.product, mr.source_text"

    with connect() as conn:
        return [
            dict(row)
            for row in conn.execute(sql).fetchall()
        ]


def get_mapping_rule(
    product: str,
    source_text: str,
) -> dict | None:
    initialize_database()

    with connect() as conn:
        row = conn.execute(
            """
            SELECT mr.*, ss.weight_spec, ss.fruit_type, ss.supplier_spec
            FROM mapping_rules mr
            JOIN standard_specs ss ON ss.id = mr.standard_spec_id
            WHERE mr.product = ?
              AND mr.source_text = ?
              AND mr.is_active = 1
              AND ss.is_active = 1
            """,
            (product, source_text),
        ).fetchone()

        return dict(row) if row else None


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

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO mapping_rules(
                product, source_text, standard_spec_id, is_active
            )
            VALUES (?, ?, ?, 1)
            ON CONFLICT(product, source_text)
            DO UPDATE SET
                standard_spec_id = excluded.standard_spec_id,
                is_active = 1,
                updated_at = CURRENT_TIMESTAMP
            """,
            (product, source_text, standard_spec_id),
        )

        row = conn.execute(
            """
            SELECT id
            FROM mapping_rules
            WHERE product = ? AND source_text = ?
            """,
            (product, source_text),
        ).fetchone()

        return int(row["id"])


def delete_mapping_rule(rule_id: int) -> None:
    initialize_database()

    with connect() as conn:
        conn.execute(
            "UPDATE mapping_rules SET is_active = 0 WHERE id = ?",
            (rule_id,),
        )
