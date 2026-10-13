from __future__ import annotations

from typing import Any

class AppError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details

def not_found(what: str) -> AppError:
    return AppError(404, "not_found", f"{what} was not found")

def conflict(code: str, message: str, details: Any = None) -> AppError:
    return AppError(409, code, message, details)

def invalid(code: str, message: str, details: Any = None) -> AppError:
    return AppError(422, code, message, details)
