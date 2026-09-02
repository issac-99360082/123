"""Signup, login and API key management.

Auth is API-key based: signup and login return a key, and every other endpoint
takes that key. No sessions, no refresh tokens - the smallest thing that is
actually secure for a developer-facing API.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import (
    check_login_lockout,
    clear_login_failures,
    client_ip,
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
    AccountExportOut,
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
def login(
    payload: LoginRequest, request: Request, db: Session = Depends(get_db)
) -> ApiKeyCreatedOut:
    """Exchange email + password for a fresh API key."""
    email = payload.email.lower().strip()
    source = client_ip(request)

    # Lock the account out after repeated failures, independently of the shared
    # per-IP limit: a distributed guesser rotates addresses, so an IP-only limit
    # is not a password-guessing control.
    check_login_lockout(email, source)

    user = db.scalar(select(User).where(User.email == email))
    # Same error and roughly the same work either way, so the response does not
    # reveal whether the email exists.
    if user is None or not verify_password(payload.password, user.password_hash):
        record_login_failure(email, source)
        raise AuthError("Invalid email or password.")
    if not user.is_active:
        record_login_failure(email, source)
        raise AuthError("Invalid email or password.")

    clear_login_failures(email, source)

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


@router.get("/me/export", response_model=AccountExportOut)
def export_account(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AccountExportOut:
    """Export everything held about this account.

    privacy.md tells users they may export their data; before this existed the
    API offered no way to honour that, which made the policy a false statement
    rather than a commitment.
    """
    from app.models import ChangeEvent, Check, Monitor

    monitors = db.scalars(select(Monitor).where(Monitor.user_id == user.id)).all()
    monitor_ids = [m.id for m in monitors]

    def _rows(model, order):
        if not monitor_ids:
            return []
        return db.scalars(
            select(model).where(model.monitor_id.in_(monitor_ids)).order_by(order.desc()).limit(5000)
        ).all()

    return AccountExportOut(
        account={
            "id": user.id,
            "email": user.email,
            "plan": user.plan,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "subscription_status": user.subscription_status,
        },
        monitors=[
            {
                "id": m.id, "name": m.name, "url": m.url, "method": m.method,
                "extract_mode": m.extract_mode, "extract_value": m.extract_value,
                "ignore_patterns": m.ignore_patterns, "interval_seconds": m.interval_seconds,
                "notify_webhook_url": m.notify_webhook_url, "notify_email": m.notify_email,
                "is_active": m.is_active,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in monitors
        ],
        checks=[
            {
                "id": c.id, "monitor_id": c.monitor_id, "status": c.status,
                "http_status": c.http_status, "error_type": c.error_type,
                "started_at": c.started_at.isoformat() if c.started_at else None,
            }
            for c in _rows(Check, Check.started_at)
        ],
        change_events=[
            {
                "id": e.id, "monitor_id": e.monitor_id, "diff": e.diff,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in _rows(ChangeEvent, ChangeEvent.created_at)
        ],
    )


@router.delete("/me", response_model=MessageOut)
def delete_account(
    confirm: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MessageOut:
    """Permanently delete this account and everything belonging to it.

    terms.md states "You may delete your account at any time"; without this
    endpoint that promise could not be kept. Requires ?confirm=<your email> so
    a leaked key cannot destroy an account with a bare DELETE.

    Any Stripe subscription must be cancelled separately through the customer
    portal - we deliberately do not cancel billing on the user's behalf here.
    """
    if confirm.strip().lower() != user.email.lower():
        raise AppError(
            "Confirmation does not match. Pass ?confirm=<your account email> to delete."
        )

    user_id = user.id
    # Monitors, keys, checks, change events and deliveries all cascade from here.
    db.delete(user)
    db.commit()
    log.info("account_deleted", user_id=user_id)
    return MessageOut(
        message=(
            "Account and all associated monitors, history and API keys deleted. "
            "Cancel any active subscription in the Stripe customer portal."
        )
    )
