"""主干的异常类型。API 层把它们映射为文档 9.1 的错误码。"""

from __future__ import annotations

from .i18n import DEFAULT_LANG, Msg, t


class AirError(Exception):
    code = "INTERNAL"
    http_status = 500

    def __init__(self, message: str = "", detail: dict | None = None, *, msg: Msg | None = None):
        """msg：可翻译的提示（代码 + 参数），API 按请求语言显示；没给时 message 原样返回。"""
        if msg is not None and not message:
            message = t(msg.code, DEFAULT_LANG, **msg.params)
        super().__init__(message)
        self.message = message
        self.detail = detail or {}
        self.msg = msg


class StaleApproval(AirError):
    code = "STALE_VERSION"
    http_status = 409


class StaleQuestion(AirError):
    code = "STALE_VERSION"
    http_status = 409


class StaleVersion(AirError):
    code = "STALE_VERSION"
    http_status = 409


class InvalidState(AirError):
    code = "INVALID_STATE"
    http_status = 409


class NotFound(AirError):
    code = "NOT_FOUND"
    http_status = 404


class PermissionDenied(AirError):
    code = "PERMISSION_DENIED"
    http_status = 403


class ValidationFailed(AirError):
    code = "VALIDATION"
    http_status = 422


class ProjectLocked(AirError):
    code = "INVALID_STATE"
    http_status = 409


class BudgetExceeded(AirError):
    """调用付费资源前发现预算已达 hard 上限。引擎收到后进入 BudgetExhausted 状态。"""

    code = "INVALID_STATE"
    http_status = 409
