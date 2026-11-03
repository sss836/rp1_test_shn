"""Verify revision 51 on a pre-existing sample in the disposable QA database."""
import os
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

url = os.environ["DATABASE_URL"]
if url.rsplit("/",1)[-1] != "rp1_presence_assistant":
    raise RuntimeError("Refuse non-isolated migration database")
config = Config("alembic.ini")
command.upgrade(config, "20261022_0050")
engine = create_engine(url)
with engine.begin() as connection:
    connection.execute(text("SELECT set_config('app.change_reason','synthetic pre-51 sample',true)"))
    identifier = connection.execute(text("""INSERT INTO test.asset(asset_code,asset_kind,product_family,serial_number,display_name)
        VALUES ('RP95.1-ARM-004','MODULE','RP95.1','SYNTHETIC-MIGRATION-SAMPLE','migration sample') RETURNING id""")).scalar_one()
    connection.execute(text("INSERT INTO test.module_profile(asset_id,module_type,target_part_code) VALUES (:id,'synthetic old model','ARM')"),{"id":identifier})
    before=dict(connection.execute(text("SELECT * FROM test.asset WHERE id=:id"),{"id":identifier}).mappings().one())
command.upgrade(config,"head")
with engine.connect() as connection:
    after=dict(connection.execute(text("SELECT * FROM test.asset WHERE id=:id"),{"id":identifier}).mappings().one())
    assert after.pop("creation_scenario")=="RND_TEST"
    assert after==before, "Migration must preserve every pre-existing sample field"
    assert connection.execute(text("SELECT count(*) FROM test.test_execution")).scalar_one()==0
engine.dispose()
print("pre-existing sample migrated to RND_TEST; all prior sample fields unchanged; no executions created")
