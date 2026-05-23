from __future__ import annotations

from typing import Callable, Iterable

from fastapi import Header, HTTPException


def require_roles(allowed: Iterable[str]) -> Callable[[str], str]:
    allowed_set = set(allowed)

    def _dependency(x_user_role: str = Header(default="viewer")) -> str:
        role = (x_user_role or "viewer").strip().lower()
        if role not in allowed_set:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "forbidden",
                    "required_roles": sorted(allowed_set),
                    "received": role,
                },
            )
        return role

    return _dependency
