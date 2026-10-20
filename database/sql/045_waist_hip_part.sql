-- Independent waist/hip identity; existing LOWER assets and facts stay intact.
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '30s';
SELECT set_config('app.change_reason', '新增独立腰胯WAISTHIP分类，不改写下肢与历史执行', true);

ALTER TABLE catalog.test_target_part
    DROP CONSTRAINT ck_test_target_part_asset_code_segment;
ALTER TABLE catalog.test_target_part
    ADD CONSTRAINT ck_test_target_part_asset_code_segment CHECK (
        part_code IN ('ARM','SARM','LEG','SLEG','SYS','UPPER','LOWER','WAISTHIP','CHEST','HEAD','BAT')
        AND asset_code_segment = part_code
    );

INSERT INTO catalog.test_target_part(part_code,name,asset_kind,description,asset_code_segment,enabled)
VALUES ('WAISTHIP','腰胯','MODULE','独立腰胯（腰髋）模块；与包含双腿的下肢LOWER分开建档','WAISTHIP',true);

ALTER TABLE test.asset DROP CONSTRAINT ck_asset_code_allowed_part_segment;
ALTER TABLE test.asset ADD CONSTRAINT ck_asset_code_allowed_part_segment CHECK (
    split_part(asset_code,'-',2) IN ('ARM','SARM','LEG','SLEG','SYS','UPPER','LOWER','WAISTHIP','CHEST','HEAD','BAT')
) NOT VALID;
ALTER TABLE test.asset VALIDATE CONSTRAINT ck_asset_code_allowed_part_segment;

ALTER TABLE test.test_execution DROP CONSTRAINT ck_execution_code_case_time_asset_station;
ALTER TABLE test.test_execution ADD CONSTRAINT ck_execution_code_case_time_asset_station CHECK (
    execution_code ~ '^[A-Z0-9]+(-[A-Z0-9]+)*_[0-9]{10}_RP[0-9]+\.[0-9]+-(ARM|SARM|LEG|SLEG|SYS|UPPER|LOWER|WAISTHIP|CHEST|HEAD|BAT)-[0-9]{3}_[A-Z0-9]+(-[A-Z0-9]+)*(_E[0-9]+)?$'
) NOT VALID;
ALTER TABLE test.test_execution VALIDATE CONSTRAINT ck_execution_code_case_time_asset_station;
