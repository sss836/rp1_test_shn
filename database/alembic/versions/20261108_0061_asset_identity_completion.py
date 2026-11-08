"""Scope-safe usage guard for completing unknown planned sample identities."""
import os
from pathlib import Path
import re

from alembic import op

revision = "20261108_0061"
down_revision = "20261105_0060"
branch_labels = None
depends_on = None


def upgrade():
    role = os.environ.get("RP1_APP_USER", "rp1_app")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", role):
        raise RuntimeError("Invalid application role")
    source = Path(__file__).resolve().parents[2] / "sql/061_asset_identity_completion.sql"
    op.get_bind().exec_driver_sql(source.read_text().replace("__RP1_APP_USER__", role).replace("%", "%%"))


def downgrade():
    op.execute("DROP FUNCTION test.asset_identity_blocked_reason(bigint)")
