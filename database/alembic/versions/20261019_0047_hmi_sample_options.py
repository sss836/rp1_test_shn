"""Expose registered sample availability without exposing other operator telemetry."""
import os
import re
from pathlib import Path

from alembic import op

revision = "20261019_0047"
down_revision = "20261019_0046"
branch_labels = None
depends_on = None


def upgrade():
    role = os.environ.get("RP1_APP_USER", "rp1_app")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", role):
        raise RuntimeError("Invalid RP1_APP_USER")
    source = Path(__file__).resolve().parents[2] / "sql/047_hmi_sample_options.sql"
    op.get_bind().exec_driver_sql(source.read_text().replace("__RP1_APP_USER__", role).replace("%", "%%"))


def downgrade():
    raise RuntimeError("Roll back the application without removing this compatible read function.")
