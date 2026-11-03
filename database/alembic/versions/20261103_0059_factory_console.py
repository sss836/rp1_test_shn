"""Factory-only operational monitoring and a scoped reviewer account."""
import os
import re
from pathlib import Path
from alembic import op
revision = '20261103_0059'
down_revision = '20261102_0058'
branch_labels = None
depends_on = None

def upgrade():
    role = os.environ.get('RP1_APP_USER', 'rp1_app')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_$]*', role):
        raise RuntimeError('Invalid application role')
    source = Path(__file__).resolve().parents[2] / 'sql/059_factory_console.sql'
    op.get_bind().exec_driver_sql(source.read_text().replace('__RP1_APP_USER__', role).replace('%', '%%'))

def downgrade():
    raise RuntimeError('Preserve reviewer audits and station scope evidence; roll back the application separately')
