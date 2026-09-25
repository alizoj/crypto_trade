"""Авторизация (раздел 4): один пользователь, пароль (argon2), сессия в httpOnly cookie."""

from __future__ import annotations

import secrets
import time
from collections import defaultdict, deque
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import HTTPException, Request, WebSocket
from sqlalchemy import delete, select

from quant.db import session_scope
from quant.db.models import AuthSession, User
from quant.timeutil import now_utc

COOKIE = "hh_session"
SESSION_TTL = timedelta(days=7)
_ph = PasswordHasher()
_fails: dict[str, deque] = defaultdict(deque)
MAX_FAILS, FAIL_WINDOW = 5, 600


def create_user(username: str, password: str) -> None:
    """Создать пользователя или сменить пароль существующему."""
    with session_scope() as s:
        u = s.scalar(select(User).where(User.username == username))
        if u is None:
            s.add(User(username=username, password_hash=_ph.hash(password)))
        else:
            u.password_hash = _ph.hash(password)


def _rate_limited(key: str) -> bool:
    q = _fails[key]
    now = time.time()
    while q and now - q[0] > FAIL_WINDOW:
        q.popleft()
    return len(q) >= MAX_FAILS


def login(username: str, password: str, client: str) -> str:
    if _rate_limited(client):
        raise HTTPException(429, "слишком много попыток, подождите")
    with session_scope() as s:
        u = s.scalar(select(User).where(User.username == username))
        ok = False
        if u is not None:
            try:
                ok = _ph.verify(u.password_hash, password)
            except VerifyMismatchError:
                ok = False
        if not ok:
            _fails[client].append(time.time())
            raise HTTPException(401, "неверный логин или пароль")
        token = secrets.token_urlsafe(48)
        s.add(AuthSession(token=token, user_id=u.id, expires_at=now_utc() + SESSION_TTL))
        s.execute(delete(AuthSession).where(AuthSession.expires_at < now_utc()))
        return token


def logout(token: str | None) -> None:
    if not token:
        return
    with session_scope() as s:
        s.execute(delete(AuthSession).where(AuthSession.token == token))


def user_for(token: str | None) -> str | None:
    if not token:
        return None
    with session_scope() as s:
        sess = s.get(AuthSession, token)
        if sess is None:
            return None
        exp = sess.expires_at if sess.expires_at.tzinfo else sess.expires_at.replace(tzinfo=now_utc().tzinfo)
        if exp < now_utc():
            return None
        u = s.get(User, sess.user_id)
        return u.username if u else None


def require_user(request: Request) -> str:
    user = user_for(request.cookies.get(COOKIE))
    if user is None:
        raise HTTPException(401, "unauthorized")
    return user


def ws_user(ws: WebSocket) -> str | None:
    return user_for(ws.cookies.get(COOKIE))
