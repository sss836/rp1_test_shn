"""Credential handling checks use a fake connection, never a database."""
import importlib.util
from pathlib import Path
from uuid import uuid4
import json
from unittest.mock import Mock
import pytest
from argon2 import PasswordHasher

spec=importlib.util.spec_from_file_location('provision_factory_reviewer',Path(__file__).resolve().parents[1]/'scripts/provision_factory_reviewer.py')
cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)


def args(monkeypatch,target,*extra):
    monkeypatch.setattr('sys.argv',['provision_factory_reviewer','--credentials-file',str(target),'--reason','Explicit synthetic request',*extra])


def test_provision_writes_a_single_private_initial_credential_and_never_prints_secret(tmp_path,monkeypatch,capsys):
    tmp_path.chmod(0o700);target=tmp_path/'new-reviewer.json'
    connection=Mock();connection.execute.return_value.scalar_one.return_value=uuid4()
    transaction=Mock();transaction.__enter__=Mock(return_value=connection);transaction.__exit__=Mock(return_value=False)
    engine=Mock();engine.begin.return_value=transaction
    factory=Mock(return_value=engine);monkeypatch.setattr(cli,'create_engine',factory)
    monkeypatch.setenv('FACTORY_PROVISION_ADMIN_DATABASE_URL','postgresql+psycopg://synthetic@127.0.0.1/never-connect')
    args(monkeypatch,target,'--apply');cli.main()
    receipt=json.loads(target.read_text())
    assert target.stat().st_mode&0o777==0o600
    assert receipt['role']=='FACTORY_REVIEWER' and receipt['must_change_password'] is True
    assert len(receipt['initial_password'])>=40
    payload=connection.execute.call_args.args[1]
    assert PasswordHasher().verify(payload['hash'],receipt['initial_password'])
    output=capsys.readouterr().out
    assert receipt['initial_password'] not in output and payload['hash'] not in output
    assert 'initial_password' not in output
    assert factory.call_args.kwargs['hide_parameters'] is True


def test_existing_credential_file_prevents_any_account_creation(tmp_path,monkeypatch):
    target=tmp_path/'existing.json';target.write_text('preserved')
    factory=Mock();monkeypatch.setattr(cli,'create_engine',factory)
    monkeypatch.setenv('FACTORY_PROVISION_ADMIN_DATABASE_URL','postgresql+psycopg://synthetic@127.0.0.1/never-connect')
    args(monkeypatch,target,'--apply')
    with pytest.raises(SystemExit):cli.main()
    assert target.read_text()=='preserved' and not factory.called
