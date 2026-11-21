"""领域异常。"""
from __future__ import annotations


class DomainError(Exception):
    """业务校验失败（文件格式错、缺表、状态不允许等），HTTP 400/404/409/422。"""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class SaveError(DomainError):
    """写入失败：事务回滚，原稿与待补记录均保持不变，重启后可继续补传。"""

    def __init__(self, message: str = "写入失败，已回滚，原稿与待补记录保持不变"):
        super().__init__(message, 500)
