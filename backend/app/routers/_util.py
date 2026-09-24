"""Shared router helpers."""

from fastapi import HTTPException


def or_404(row, what: str = "Resource"):
    if row is None:
        raise HTTPException(404, f"{what} not found")
    return row
