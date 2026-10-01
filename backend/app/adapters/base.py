from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class AdapterError(RuntimeError):
    def __init__(self, code: str, message: str, action: str = "", details: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.action = action
        self.details = details or {}


class BlockedError(AdapterError):
    pass


class RemoteServiceError(AdapterError):
    pass


@dataclass
class ServiceStatus:
    status: str
    detail: str = ""
    base_url: str | None = None
    model: str | None = None

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"status": self.status, "detail": self.detail}
        if self.base_url:
            value["base_url"] = self.base_url
        if self.model:
            value["model"] = self.model
        return value
