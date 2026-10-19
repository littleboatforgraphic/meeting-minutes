"""Login: password, one-time code (OTP), or both (2-step), chosen per company. Long-lived device sessions."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import __version__
from .company import CompanyConfig
from .db import AuthSession, LinkToken, LoginChallenge, Person, utcnow
from .directory import normalize_phone
from .notify import Dispatcher, Message

COOKIE = "mm_session"
OTP_MINUTES = 10
OTP_MAX_ATTEMPTS = 5
_ph = PasswordHasher()


class AuthError(Exception):
    pass


def hash_password(pw: str) -> str:
    if len(pw) < 8:
        raise AuthError("password must be at least 8 characters")
    return _ph.hash(pw)


def verify_password(person: Person, pw: str) -> bool:
    if not person.password_hash:
        return False
    try:
        return _ph.verify(person.password_hash, pw)
    except VerifyMismatchError:
        return False


def _digest(secret: str, value: str) -> str:
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()


def find_person(db: Session, identifier: str) -> Person | None:
    ident = identifier.strip()
    digits = normalize_phone(ident) if not ident.startswith("@") else None
    q = select(Person).where(Person.active.is_(True))
    if "@" in ident:
        return db.scalar(q.where(func.lower(Person.email) == ident.lower()))
    if digits and len(digits) >= 9 and not any(c.isalpha() for c in ident):
        return db.scalar(q.where(Person.phone == digits))
    return db.scalar(q.where(func.lower(Person.telegram_username) == ident.lstrip("@").lower()))


def start_otp(db: Session, company: CompanyConfig, dispatcher: Dispatcher, secret: str, person: Person) -> str:
    code = f"{secrets.randbelow(10**6):06d}"
    cid = secrets.token_urlsafe(24)
    db.add(LoginChallenge(id=cid, person_id=person.id, code_hash=_digest(secret, f"{cid}:{code}"),
                          expires_at=utcnow() + timedelta(minutes=OTP_MINUTES)))
    db.flush()
    msg = Message(subject=f"{company.company.name} login code",
                  text=f"Your login code is {code}. It expires in {OTP_MINUTES} minutes. Do not share it.")
    if not dispatcher.send_first(db, person, msg, "otp", list(company.auth.otp_channels)):
        raise AuthError("no channel available to send a login code; ask your admin")
    return cid


def verify_otp(db: Session, secret: str, challenge_id: str, code: str) -> Person:
    ch = db.get(LoginChallenge, challenge_id)
    if not ch or ch.used or ch.expires_at < utcnow() or ch.attempts >= OTP_MAX_ATTEMPTS:
        raise AuthError("code expired; request a new one")
    ch.attempts += 1
    if not hmac.compare_digest(ch.code_hash, _digest(secret, f"{challenge_id}:{code.strip()}")):
        db.flush()
        raise AuthError("wrong code")
    ch.used = True
    db.flush()
    return db.get(Person, ch.person_id)


def create_session(db: Session, company: CompanyConfig, secret: str, person: Person) -> str:
    token = secrets.token_urlsafe(32)
    db.add(AuthSession(token_hash=_digest(secret, token), person_id=person.id, app_version=__version__,
                       expires_at=utcnow() + timedelta(days=company.auth.session_days)))
    db.flush()
    return token


def session_person(db: Session, company: CompanyConfig, secret: str, token: str | None) -> Person | None:
    if not token:
        return None
    s = db.scalar(select(AuthSession).where(AuthSession.token_hash == _digest(secret, token)))
    now = utcnow()
    if not s or s.expires_at < now or not s.person.active:
        return None
    if company.auth.logout_on_app_update and s.app_version != __version__:
        db.delete(s)
        db.flush()
        return None
    if (now - s.last_seen_at) > timedelta(minutes=5):
        s.last_seen_at = now
        if company.auth.remember_device:  # sliding expiry
            s.expires_at = now + timedelta(days=company.auth.session_days)
        db.flush()
    return s.person


def end_session(db: Session, secret: str, token: str | None) -> None:
    if token:
        s = db.scalar(select(AuthSession).where(AuthSession.token_hash == _digest(secret, token)))
        if s:
            db.delete(s)
            db.flush()


def create_link_token(db: Session, secret: str, person: Person, purpose: str, days: int = 7) -> str:
    token = secrets.token_urlsafe(24)
    db.add(LinkToken(token_hash=_digest(secret, token), person_id=person.id, purpose=purpose,
                     expires_at=utcnow() + timedelta(days=days)))
    db.flush()
    return token


def use_link_token(db: Session, secret: str, token: str, purpose: str) -> Person:
    lt = db.scalar(select(LinkToken).where(LinkToken.token_hash == _digest(secret, token), LinkToken.purpose == purpose))
    if not lt or lt.used_at or lt.expires_at < utcnow():
        raise AuthError("link expired or already used")
    lt.used_at = utcnow()
    db.flush()
    return db.get(Person, lt.person_id)
