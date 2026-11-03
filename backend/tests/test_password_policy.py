"""The operator-selected six-character policy, without changing authentication."""
from uuid import uuid4

import pytest
from argon2 import PasswordHasher, Type
from pydantic import ValidationError

from app.core.config import Settings
from app.schemas.security import ChangePasswordRequest, Principal, UserCreateRequest
from app.services.auth import AuthService


@pytest.mark.parametrize("password", ["", "1", "12345"])
def test_create_change_and_seed_reject_below_six(password):
    with pytest.raises(ValidationError):
        UserCreateRequest(username="operator", display_name="Operator", role="TEST_EXECUTOR", password=password)
    with pytest.raises(ValidationError):
        ChangePasswordRequest(current_password="previous-password", new_password=password)
    with pytest.raises(ValidationError, match="6"):
        Settings(_env_file=None, seed_admin_username="operator", seed_admin_password=password)


@pytest.mark.parametrize("password", ["123456", "existing-strong-password-2026!"])
def test_create_change_and_seed_accept_six_and_existing_strong_passwords(password):
    assert UserCreateRequest(username="operator", display_name="Operator", role="TEST_EXECUTOR", password=password).password == password
    assert ChangePasswordRequest(current_password="previous-password", new_password=password).new_password == password
    assert Settings(_env_file=None, seed_admin_username="operator", seed_admin_password=password).seed_admin_password.get_secret_value() == password


class PasswordRepository:
    def __init__(self, user_id, hasher):
        self.user_id = user_id
        self.hasher = hasher
        self.saved = []

    def lookup_login_credential(self, username):
        return {"user_public_id": self.user_id, "password_hash": self.hasher.hash("previous-password")}

    def change_current_password(self, *args):
        self.saved.append(args)


def test_normal_authenticated_password_change_accepts_six_and_keeps_role():
    hasher = PasswordHasher(type=Type.ID, time_cost=1, memory_cost=8192, parallelism=1)
    user_id, session_id = uuid4(), uuid4()
    repo = PasswordRepository(user_id, hasher)
    service = AuthService(Settings(_env_file=None), repo, password_hasher=hasher)
    principal = Principal(id=user_id, username="operator", display_name="Operator", role="TEST_EXECUTOR", kind="HUMAN", session_type="BROWSER", session_id=session_id, must_change_password=True)
    result = service.change_password(principal, "previous-password", "123456", "unit-password-change", "127.0.0.1")
    assert result.role == "TEST_EXECUTOR"
    assert result.must_change_password is False
    assert len(repo.saved) == 1
    assert repo.saved[0][0] == session_id
    assert hasher.verify(repo.saved[0][1], "123456")


def test_service_rejects_five_without_saving_and_schema_keeps_same_password_guard():
    hasher = PasswordHasher(type=Type.ID, time_cost=1, memory_cost=8192, parallelism=1)
    user_id = uuid4()
    repo = PasswordRepository(user_id, hasher)
    service = AuthService(Settings(_env_file=None), repo, password_hasher=hasher)
    principal = Principal(id=user_id, username="operator", display_name="Operator", role="TEST_EXECUTOR", kind="HUMAN", session_type="BROWSER", session_id=uuid4())
    with pytest.raises(ValueError, match="6"):
        service.change_password(principal, "previous-password", "12345", "unit-password-change", None)
    assert repo.saved == []
    with pytest.raises(ValidationError, match="differ"):
        ChangePasswordRequest(current_password="123456", new_password="123456")
