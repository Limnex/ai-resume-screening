"""提供不依赖 FastAPI 或 HTTP 的统一应用异常。

数据流：领域/应用/存储层发现失败 -> 抛出 ``AppError`` -> API 异常处理器统一响应。
原理：稳定错误码适合程序判断，消息和详情则供用户理解与排查。
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """与传输协议无关的应用错误。

    过渡期兼容旧的 ``(status_code, code, message, details)`` 调用，HTTP 状态码
    不再保存在异常中，而是统一由 API 边界根据稳定错误码映射。
    """

    def __init__(self, *args: Any) -> None:
        """兼容新旧参数形式，并统一保存错误码、消息与结构化详情。"""
        if args and isinstance(args[0], int):
            _, code, message, *remaining = args
        else:
            code, message, *remaining = args
        details = remaining[0] if remaining else None
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.details = details if isinstance(details, dict) else {}
