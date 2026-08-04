from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from database import supabase_backend as cloud


def migrate(sqlite_path: Path) -> None:
    if not sqlite_path.exists():
        raise FileNotFoundError(f"找不到数据库文件：{sqlite_path}")

    cloud.initialize_database()

    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row

    products = conn.execute(
        "SELECT name FROM products WHERE is_active = 1 ORDER BY id"
    ).fetchall()
    for product_row in products:
        name = str(product_row["name"])
        weights = [
            str(row["weight_spec"])
            for row in conn.execute(
                """
                SELECT pw.weight_spec
                FROM product_weights pw
                JOIN products p ON p.id = pw.product_id
                WHERE p.name = ? AND pw.is_active = 1
                ORDER BY pw.id
                """,
                (name,),
            ).fetchall()
        ]
        fruits = [
            str(row["fruit_type"])
            for row in conn.execute(
                """
                SELECT pf.fruit_type
                FROM product_fruit_types pf
                JOIN products p ON p.id = pf.product_id
                WHERE p.name = ? AND pf.is_active = 1
                ORDER BY pf.id
                """,
                (name,),
            ).fetchall()
        ]
        cloud.merge_product_definition(name, weights, fruits)

    spec_rows = conn.execute(
        """
        SELECT id, product, weight_spec, fruit_type
        FROM standard_specs
        WHERE is_active = 1
        ORDER BY id
        """
    ).fetchall()
    spec_id_map: dict[int, int] = {}
    for row in spec_rows:
        cloud_id = cloud.add_standard_spec(
            str(row["product"]),
            str(row["weight_spec"]),
            str(row["fruit_type"]),
        )
        spec_id_map[int(row["id"])] = cloud_id

    rule_rows = conn.execute(
        """
        SELECT product, source_text, standard_spec_id
        FROM mapping_rules
        WHERE is_active = 1
        ORDER BY id
        """
    ).fetchall()
    migrated_rules = 0
    for row in rule_rows:
        cloud_spec_id = spec_id_map.get(int(row["standard_spec_id"]))
        if cloud_spec_id is None:
            continue
        cloud.save_mapping_rule(
            str(row["product"]),
            str(row["source_text"]),
            cloud_spec_id,
        )
        migrated_rules += 1

    print(f"产品迁移完成：{len(products)} 个")
    print(f"标准规格迁移完成：{len(spec_rows)} 条")
    print(f"映射规则迁移完成：{migrated_rules} 条")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="将本地 ruleflow.db 迁移到 Supabase。"
    )
    parser.add_argument(
        "sqlite_path",
        nargs="?",
        default="ruleflow.db",
        help="本地 SQLite 文件路径，默认 ruleflow.db",
    )
    args = parser.parse_args()
    migrate(Path(args.sqlite_path).expanduser().resolve())
