"""Add independent waist/hip classification without relabeling historical facts."""
from pathlib import Path

from alembic import op

revision = "20261019_0045"
down_revision = "20261017_0044"
branch_labels = None
depends_on = None


def upgrade():
    source = Path(__file__).resolve().parents[2] / "sql/045_waist_hip_part.sql"
    op.get_bind().exec_driver_sql(source.read_text().replace("%", "%%"))


def downgrade():
    raise RuntimeError("WAISTHIP identifiers may be referenced by samples and executions; retain the additive classification when rolling back application images.")
