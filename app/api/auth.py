"""Signup, login and API key management.

Auth is API-key based: signup and login return a key, and every other endpoint
takes that key. No sessions, no refresh tokens - the smallest thing that is
actually secure for a developer-facing API.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import (
    check_login_lockout,
    clear_login_failures,
    get_current_user,
    rate_limit,
    record_login_failure,
    signup_rate_limit,
)
from app.errors import AppError, AuthError, NotFoundError
from app.logging_conf import get_logger
from app.models import ApiKey, User
from app.plans import DEFAULT_PLAN
from app.schemas import (
    ApiKeyCreatedOut,
    ApiKeyCreateRequest,
    ApiKeyOut,
    LoginRequest,
    MessageOut,
    SignupRequest,
    UserOut,
)
from app.security import generate_api_key, hash_password, verify_password

router = APIRouter(prefix="/v1/auth", tags=["auth"], dependencies=[Depends(rate_limit)])
log = get_logger(__name__)

MAX_KEYS_PER_USER = 10


def _issue_key(db: Session, user: User, name: str) -> tuple[ApiKey, str]:
    plaintext, prefix, key_hash = generate_api_key()
    api_key = ApiKey(user_id=user.id, name=name[:100], prefix=prefix, key_hash=key_hash)
    db.add(api_key)
    db.commit()
    db.refresh(api_key)
    return api_key, plaintext


@router.post(
    "/signup",
    response_model=ApiKeyCreatedOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(signup_rate_limit)],
)
def signup(payload: SignupRequest, db: Session = Depends(get_db)) -> ApiKeyCreatedOut:
    """Create an account and return its first API key. Self-serve, no human in the loop."""
    email = payload.email.lower().strip()
    if db.scalar(select(User).where(User.email == email)):
        # Deliberately vague. NOTE: this only narrows the enumeration oracle,
        # it does not close it - the status code still differs from a successful
        # signup. Closing it fully requires an email-verification flow that
        # returns an identical 202 in both cases, which needs SMTP configured.
        # Tracked in docs/SECURITY.md.
        raise AppError("Unable to create an account with those details.")

    user = User(email=email, password_hash=hash_password(payload.password), plan=DEFAULT_PLAN)
    db.add(user)
    db.commit()
    db.refresh(user)

    api_key, plaintext = _issue_key(db, user, "default")
    log.info("user_signed_up", user_id=user.id)
    return ApiKeyCreatedOut(**ApiKeyOut.model_validate(api_key).model_dump(), key=plaintext)


@router.post("/login", response_model=ApiKeyCreatedOut)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> ApiKeyCreatedOut:
    """Exchange email + password for a fresh API key."""
    email = payload.email.lower().strip()

    # Lock the account out after repeated failures, independently of the shared
    # per-IP limit: a distributed guesser rotates addresses, so an IP-only limit
    # is not a password-guessing control.
    check_login_lockout(email)

    user = db.scalar(select(User).where(User.email == email))
    # Same error and roughly the same work either way, so the response does not
    # reveal whether the email exists.
    if user is None or not verify_password(payload.password, user.password_hash):
        record_login_failure(email)
        raise AuthError("Invalid email or password.")
    if not user.is_active:
        record_login_failure(email)
        raise AuthError("Invalid email or password.")

    clear_login_failures(email)

    active = db.scalars(
        select(ApiKey).where(ApiKey.user_id == user.id, ApiKey.revoked_at.is_(None))
    ).all()
    if len(active) >= MAX_KEYS_PER_USER:
        raise AppError(
            f"You already have {MAX_KEYS_PER_USER} active API keys. Revoke one before creating another."
        )

    api_key, plaintext = _issue_key(db, user, "login")
    log.info("user_logged_in", user_id=user.id)
    return ApiKeyCreatedOut(**ApiKeyOut.model_validate(api_key).model_dump(), key=plaintext)


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut.model_validate(user)


@router.get("/keys", response_model=list[ApiKeyOut])
def list_keys(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[ApiKeyOut]:
    keys = db.scalars(
        select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.created_at.desc())
    ).all()
    return [ApiKeyOut.model_validate(k) for k in keys]


@router.post("/keys", response_model=ApiKeyCreatedOut, status_code=status.HTTP_201_CREATED)
def create_key(
    payload: ApiKeyCreateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiKeyCreatedOut:
    active = db.scalars(
        select(ApiKey).where(ApiKey.user_id == user.id, ApiKey.revoked_at.is_(None))
    ).all()
    if len(active) >= MAX_KEYS_PER_USER:
        raise AppError(f"Maximum of {MAX_KEYS_PER_USER} active API keys reached.")

    api_key, plaintext = _issue_key(db, user, payload.name)
    return ApiKeyCreatedOut(**ApiKeyOut.model_validate(api_key).model_dump(), key=plaintext)


@router.delete("/keys/{key_id}", response_model=MessageOut)
def revoke_key(
    key_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MessageOut:
    api_key = db.scalar(select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user.id))
    if api_key is None:
        raise NotFoundError(f"API key {key_id} not found.")
    if api_key.revoked_at is None:
        api_key.revoked_at = datetime.now(timezone.utc)
        db.commit()
    return MessageOut(message=f"API key {key_id} revoked.")
