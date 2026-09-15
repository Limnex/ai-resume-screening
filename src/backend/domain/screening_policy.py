"""实现不依赖框架的确定性筛选规则，目前聚焦工作年限抽取。

数据流：原始 JD/简历文本 -> 文本规范化与正则匹配 -> 最低年限或候选人年限。
原理：可明确计算的硬条件由代码处理，避免把简单数值判断完全交给概率模型。
"""

from __future__ import annotations

import re
from typing import Any


def normalize_text(value: Any) -> str:
    """统一空白与大小写，使规则匹配不受展示格式干扰。"""
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def extract_jd_experience_minimum(jd_text: str) -> float | None:
    """从 JD 明确要求中提取最高的最低年限，没有门槛则返回空值。"""
    patterns = (
        r"(?:required\s+experience|experience(?:\s+required)?)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*\+?\s*years?",
        r"(\d+(?:\.\d+)?)\s*年(?:以上|及以上|或以上)?(?:相关)?(?:工作)?经验",
    )
    values = [float(match.group(1)) for pattern in patterns for match in re.finditer(pattern, jd_text, re.I)]
    return max(values) if values else None


def find_resume_experience(resume_text: str) -> tuple[float, str] | None:
    """从简历找出最大工作年限及对应原句，供数值判断和证据展示。"""
    patterns = (
        r"[^\n。]*(\d+(?:\.\d+)?)\s*\+?\s*years?(?:\s+of)?(?:[^\n。]{0,40}?)\s+experience[^\n。]*",
        r"[^\n。]*(\d+(?:\.\d+)?)\s*年(?:以上|及以上|或以上)?[^\n。]{0,20}?经验[^\n。]*",
    )
    candidates: list[tuple[float, str]] = []
    for pattern in patterns:
        for match in re.finditer(pattern, resume_text, re.I):
            candidates.append((float(match.group(1)), match.group(0).strip()))
    return max(candidates, key=lambda item: item[0]) if candidates else None
