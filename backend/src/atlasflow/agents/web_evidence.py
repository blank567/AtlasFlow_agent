"""Shared web-source policy; capability choice remains semantic, not regex routing."""

from collections.abc import Mapping
from typing import Any


WEB_SOURCE_PLANNING_RULES = (
    "网页资料任务采用搜索优先、原文按需的策略。目录包含web_search时，"
    "景点介绍、历史沿革、概念背景、一般资料整理通常只要求web_search；"
    "不要惯性把web_fetch与web_search同时列为必需，也不要为使用web_fetch增设成功标准。"
    "web_fetch继续作为Researcher可自主选择的工具，无须在任务中声明为必需才能调用。"
    "只有用户明确要求读取指定网页、逐字引用、全文/表格细节，或目标确实必须直接核对"
    "原文且搜索证据不能替代时，才将原文读取能力设为必需，并在objective或success_criteria"
    "说明具体核对内容。用户明确的原文阅读要求不得擅自降为搜索摘要。"
    "时效要求不等于必须抓取网页；真实且足够具体的当前官方搜索证据也可用于核验。"
)

WEB_SOURCE_EXECUTION_RULES = (
    "一般网页资料先用web_search，摘要、来源片段已足以支持核心结论时直接停止。"
    "必需能力是最低要求而非固定流水线；只有需要补齐具体细节、解决来源矛盾、"
    "核对原话/表格或读取指定正文时，才按需追加web_fetch。"
    "可选web_fetch访问失败但已有搜索证据可用时，保留实际失败和未核实范围，"
    "换可访问来源或结束工具阶段，不为凑齐抓取次数反复请求，不把可选失败改成任务硬要求。"
    "明确声明的必需原文读取能力仍须真实执行，不能用搜索成功冒充完成。"
)

WEB_EVIDENCE_RULES = (
    "区分搜索摘要/搜索服务返回的来源片段与本工具直接读取的网页原文片段。"
    "有引用链接不等于已读原文，取得片段也不等于阅读全文；不得声称已全文核验、"
    "逐字引用未取得的内容，或把搜索概述写成官网当日承诺。"
    "按证据是否实际支持结论判断充分性；未使用web_fetch本身不是缺陷，"
    "不要仅因没有直接抓取而降低已有依据结论的评分、强制补充研究或拒收一般介绍报告。"
    "用户明确要求原文核对或仍有关键事实缺口时，应如实保留未完成项。"
)


def evidence_access_label(metadata: Mapping[str, Any]) -> str:
    """Describe how THIS excerpt was obtained, not a probability of truth."""

    provider = metadata.get("provider")
    kind = metadata.get("content_kind")
    if provider == "http" and kind == "source_excerpt":
        return "直接读取网页的原文片段（不等于阅读全文）"
    if provider == "openrouter" and kind == "source_excerpt":
        return "搜索服务返回的来源片段（非本工具直接读取）"
    if kind == "search_response_citation":
        return "搜索摘要/回答中的引用片段（不能冒充网页原文）"
    return "获取方式未标注（不能据此声称已直接读取原网页）"


def evidence_access_priority(metadata: Mapping[str, Any]) -> int:
    """Prefer an actually read excerpt for the same URL; not a truth score."""

    if metadata.get("content_kind") == "source_excerpt":
        if metadata.get("provider") == "http":
            return 3
        if metadata.get("provider") == "openrouter":
            return 2
    return 1 if metadata.get("content_kind") == "search_response_citation" else 0
