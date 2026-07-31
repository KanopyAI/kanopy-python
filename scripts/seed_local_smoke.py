"""Seed a disposable SDK smoke-test identity and print its API key as JSON.

This script is executed inside the isolated backend container. The raw key is
never persisted; the database stores only its hash, just like the public API.
"""

from datetime import UTC, datetime, timedelta

from app.core.security import generate_bearer_token, hash_bearer_token
from app.db.models import AuthToken, PlatformRole, User
from app.db.session import control_engine
from app.services.accounts.access_policy import apply_platform_role
from app.services.accounts.organizations import (
    KANOPY_ADMIN_ORG_ID,
    ensure_membership,
    ensure_system_organizations,
)
from sqlmodel import Session, select

EMAIL = "sdk-smoke@kanopy.local"


with Session(control_engine) as session:
    ensure_system_organizations(session)
    user = session.exec(select(User).where(User.email == EMAIL)).first()
    if user is None:
        user = User(
            email=EMAIL,
            password_hash="local-smoke-test-no-login",
            full_name="SDK smoke test",
            is_active=True,
            email_verified=True,
            default_organization_id=KANOPY_ADMIN_ORG_ID,
        )
    apply_platform_role(user, PlatformRole.ADMIN)
    session.add(user)
    session.commit()
    session.refresh(user)
    ensure_membership(
        session,
        organization_id=KANOPY_ADMIN_ORG_ID,
        user_id=user.id,
        role="admin",
    )

    raw_token = generate_bearer_token()
    session.add(
        AuthToken(
            user_id=user.id,
            token_hash=hash_bearer_token(raw_token),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            is_api_key=True,
            name="isolated local SDK smoke test",
        )
    )
    session.commit()

print('{"api_key":"' + raw_token + '"}')
