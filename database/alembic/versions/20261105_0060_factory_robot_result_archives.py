"""Dedicated factory robot result-summary evidence and read-only robot cards."""
import os
from pathlib import Path
import re

from alembic import op

revision = "20261105_0060"
down_revision = "20261103_0059"
branch_labels = None
depends_on = None


def upgrade():
    role = os.environ.get("RP1_APP_USER", "rp1_app")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", role):
        raise RuntimeError("Invalid application role")
    source = Path(__file__).resolve().parents[2] / "sql/060_factory_robot_result_archives.sql"
    op.get_bind().exec_driver_sql(source.read_text().replace("__RP1_APP_USER__", role).replace("%", "%%"))


def downgrade():
    raise RuntimeError("Preserve factory CSV evidence, source bindings, versions and review audits")
