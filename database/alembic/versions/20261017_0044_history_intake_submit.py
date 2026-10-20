"""Combine explicit history verification, classification and submission atomically."""
import os
import re
from pathlib import Path

from alembic import op

revision = "20261017_0044"
down_revision = "20261017_0043"
branch_labels = None
depends_on = None


def upgrade():
    role = os.environ.get("RP1_APP_USER", "rp1_app")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", role):
        raise RuntimeError("Invalid RP1_APP_USER")
    source = Path(__file__).resolve().parents[2] / "sql/044_history_intake_submit.sql"
    op.get_bind().exec_driver_sql(source.read_text().replace("__RP1_APP_USER__", role).replace("%", "%%"))


def downgrade():
    raise RuntimeError("History intake receipts and frozen approval evidence must be retained.")
