"""Single verified Scene identity and owner-authorization contract.

Injected resolvers remain available for isolated integration tests, never from an
HTTP header. In deployed mode, only revocable, Scene-verified SSO may log in.
"""
from dataclasses import dataclass
import inspect
from typing import Awaitable, Callable, Optional, Union

from fastapi import Depends, HTTPException, Request


@dataclass(frozen=True, slots=True)
class Principal:
    username: str
    is_admin: bool = False
    is_active: bool = False
    concat_granted: bool = False

    def valid(self) -> bool:
        from app.modules.admin.security import _valid_username
        return _valid_username(self.username) and self.username.casefold() != "__default__"


IdentityResolver = Callable[[Request], Union[Optional[Principal], Awaitable[Optional[Principal]]]]


async def current_principal(request: Request) -> Principal:
    resolver: Optional[IdentityResolver] = getattr(request.app.state, "identity_resolver", None)
    if resolver is not None:
        principal = resolver(request)
        if inspect.isawaitable(principal):
            principal = await principal
    else:
        # Identity is never inferred from X-Auth-User, forwarded headers,
        # browser JSON, a user-supplied owner field or the Scene session cookie.
        from app.modules.admin.security import SecurityService, SecurityUnavailable
        try:
            service = getattr(request.app.state, "concat_security", None)
            if service is None:
                service = SecurityService.from_environment(request.app)
                request.app.state.concat_security = service
            principal = service.authenticate(request)
        except SecurityUnavailable:
            raise HTTPException(401, "Verified Scene authentication unavailable") from None
    if not isinstance(principal, Principal) or not principal.valid() or not principal.is_active:
        raise HTTPException(status_code=401, detail="Active authenticated session required")
    if not principal.concat_granted:
        raise HTTPException(status_code=403, detail="Concat Web access not granted")
    return principal


async def admin_principal(principal: Principal = Depends(current_principal)) -> Principal:
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="Administrator access required")
    return principal


def require_owned_resource(principal: Principal, row, app: str):
    """Common predicate for Agent 2/3/4 APIs: admins have no owner bypass."""
    from app.modules.admin.security import ensure_owner
    return ensure_owner(principal, row, app)
