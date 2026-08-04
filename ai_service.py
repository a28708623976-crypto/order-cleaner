from __future__ import annotations

import json
import os
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

API_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"


class AIConfigurationError(RuntimeError):
    pass


def get_api_key() -> str:
    return os.getenv("ZHIPUAI_API_KEY", "").strip()


def get_model() -> str:
    return os.getenv("ZHIPUAI_MODEL", "glm-4.7-flash").strip() or "glm-4.7-flash"


def is_ai_configured() -> bool:
    return bool(get_api_key())


def _safe_json_loads(content: str) -> dict[str, Any]:
    text = (content or "").strip()

    if text.startswith("```json"):
        text = text[len("```json"):].strip()
    elif text.startswith("```"):
        text = text[len("```"):].strip()

    if text.endswith("```"):
        text = text[:-3].strip()

    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "智谱返回的内容不是有效JSON，请重新分析一次。"
        ) from exc

    if not isinstance(result, dict):
        raise RuntimeError("智谱返回的数据结构不正确。")

    return result


def _extract_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        return response.text[:300] or f"HTTP {response.status_code}"

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)
        if error:
            return str(error)

        message = payload.get("message")
        if message:
            return str(message)

    return str(payload)[:300]


def analyze_sku(
    *,
    source_text: str,
    detected_product: str,
    available_specs: list[str],
) -> dict[str, Any]:
    api_key = get_api_key()

    if not api_key:
        raise AIConfigurationError(
            "尚未配置智谱API Key，请检查项目根目录中的 .env 文件。"
        )
    if not source_text.strip():
        raise ValueError("原始规格文本不能为空。")
    if not available_specs:
        raise ValueError(
            "当前产品没有可选标准规格，请先在规格资料库补充。"
        )

    system_prompt = """
你是农产品电商SKU规格审核助手。你只负责提供审核建议，不修改订单。

必须遵守：
1. recommended_spec 只能从 available_specs 中选择，无法判断则返回空字符串。
2. 商品标题前部可能包含营销词，实际SKU常在后半段，但不能机械地只取最后一个词。
3. 必须区分源规格词和最终标准规格。
4. 不得虚构产品、重量、果型或供应规格。
5. 有冲突、歧义、信息不足时，needs_human_confirmation 必须为 true。
6. confidence 是 0 到 1 之间的数字。
7. suggested_keywords 只保留能够稳定复用且不会过度泛化的关键词。
8. 只返回JSON对象，不返回Markdown和额外说明。
""".strip()

    user_payload = {
        "source_text": source_text,
        "detected_product": detected_product,
        "available_specs": available_specs,
        "required_output": {
            "product_candidate": "字符串",
            "sku_fragment": "实际规格片段",
            "recommended_spec": "available_specs中的一项或空字符串",
            "alternative_specs": ["available_specs中的候选"],
            "confidence": 0.0,
            "needs_human_confirmation": True,
            "reason": "简短中文判断理由",
            "suggested_keywords": ["核心规格关键词"],
        },
    }

    request_body = {
        "model": get_model(),
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(user_payload, ensure_ascii=False),
            },
        ],
        "temperature": 0.1,
        "stream": False,
        "response_format": {"type": "json_object"},
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=60.0) as client:
            response = client.post(
                API_URL,
                headers=headers,
                json=request_body,
            )
    except httpx.TimeoutException as exc:
        raise RuntimeError("智谱请求超时，请稍后重试。") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"智谱网络请求失败：{exc}") from exc

    if response.status_code >= 400:
        message = _extract_error_message(response)

        if response.status_code == 401:
            raise RuntimeError(f"API Key无效或未授权：{message}")
        if response.status_code == 429:
            raise RuntimeError(f"调用频率超限，请稍后重试：{message}")

        raise RuntimeError(
            f"智谱接口返回错误（HTTP {response.status_code}）：{message}"
        )

    try:
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("智谱接口返回结构异常。") from exc

    result = _safe_json_loads(content)

    recommended = str(result.get("recommended_spec", "") or "").strip()
    if recommended and recommended not in available_specs:
        result["recommended_spec"] = ""
        result["needs_human_confirmation"] = True
        result["reason"] = "AI建议不在标准规格库中，系统已自动拦截。"

    alternatives = result.get("alternative_specs", [])
    if not isinstance(alternatives, list):
        alternatives = []
    result["alternative_specs"] = [
        str(item)
        for item in alternatives
        if str(item) in available_specs
    ]

    try:
        confidence = float(result.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    result["confidence"] = max(0.0, min(1.0, confidence))

    result["needs_human_confirmation"] = bool(
        result.get("needs_human_confirmation", True)
    )

    keywords = result.get("suggested_keywords", [])
    if not isinstance(keywords, list):
        keywords = []
    result["suggested_keywords"] = [
        str(item).strip()
        for item in keywords
        if str(item).strip()
    ][:8]

    return result
