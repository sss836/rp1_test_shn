"""Account chat retention and durable task/budget receipts."""
import os
import re
from pathlib import Path
from alembic import op

revision = "20261022_0049"
down_revision = "20261022_0048"
branch_labels = None
depends_on = None


def upgrade():
    role = os.environ.get("RP1_APP_USER", "rp1_app")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", role):
        raise RuntimeError("Invalid application role")
    source = Path(__file__).resolve().parents[2] / "sql/049_assistant_runtime.sql"
    op.get_bind().exec_driver_sql(source.read_text().replace("__RP1_APP_USER__", role).replace("%", "%%"))


def downgrade():
    raise RuntimeError("Preserve task, accounting and operation evidence; roll back application separately")
