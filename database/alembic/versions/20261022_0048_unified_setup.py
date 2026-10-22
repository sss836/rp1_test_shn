"""Creator permissions, immutable authored case versions and atomic setup receipts."""
import os
import re
from pathlib import Path
from alembic import op

revision = "20261022_0048"
down_revision = "20261019_0047"
branch_labels = None
depends_on = None


def upgrade():
    role = os.environ.get("RP1_APP_USER", "rp1_app")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", role):
        raise RuntimeError("Invalid RP1_APP_USER")
    sql = Path(__file__).resolve().parents[2] / "sql/048_unified_setup.sql"
    op.get_bind().exec_driver_sql(sql.read_text().replace("__RP1_APP_USER__", role).replace("%", "%%"))


def downgrade():
    raise RuntimeError("Retain creator and receipt evidence; roll back application separately.")
