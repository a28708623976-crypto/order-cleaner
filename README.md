# 智洗表 V2.3

订单 Excel/CSV 清洗、规格审核、规则复用与智谱 AI 辅助工具。

## 当前能力

- 产品主数据资料库
- 重量规格与果型批量维护
- 自动生成供应规格组合
- 上传 Excel / CSV
- 按唯一“选购商品”扫描
- 历史映射规则优先复用
- 人工选择直接用于本次清洗
- 可选保存为复用规则
- 智谱 AI 辅助判断待审核规格
- 导出固定 17 列 Excel

## Streamlit Community Cloud 部署

部署参数：

```text
Repository: a28708623976-crypto/order-cleaner
Branch: main
Main file path: app.py
```

在 Advanced settings → Secrets 中配置：

```toml
ZHIPUAI_API_KEY = "你的完整智谱 API Key"
ZHIPUAI_MODEL = "glm-4-flash-250414"
```

不要把 `.env` 或真实 API Key 上传到 GitHub。

## 本地运行

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m streamlit run app.py
```

## 数据持久化说明

当前规则库使用 SQLite，文件名为 `ruleflow.db`。本地运行时可以持久保存；Streamlit Community Cloud 重启或重新部署后，线上新增数据可能丢失。正式长期使用时建议迁移到 Supabase 等云数据库。
