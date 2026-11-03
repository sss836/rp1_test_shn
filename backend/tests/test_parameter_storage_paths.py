"""Parameter store path guards in checkout and actual container layouts."""
from pathlib import Path

import pytest
from app.core.config import Settings
from app.core.errors import ApiError
from app.services import motor_parameters as module


def settings(root,**changes):
    return Settings(_env_file=None,motor_parameters_enabled=True,motor_parameters_root=str(root),**changes)


@pytest.mark.parametrize('location,boundary',[
    ('/app/app/services/motor_parameters.py','/app'),
    ('/source/backend/app/services/motor_parameters.py','/source'),
])
def test_valid_external_store_and_repository_ancestors_rejected(tmp_path,monkeypatch,location,boundary):
    monkeypatch.setattr(module,'__file__',location)
    store=module.ParameterTemplates(settings(tmp_path/'parameters'))
    assert store.root==tmp_path/'parameters'
    for root in (boundary,str(Path(boundary)/'nested'),'/'):
        with pytest.raises(ApiError) as error:module.ParameterTemplates(settings(root))
        assert error.value.status_code==503 and error.value.code=='parameter_templates_path'


@pytest.mark.parametrize('name',['archive_root','hmi_updates_root','motor_firmware_root'])
@pytest.mark.parametrize('relationship',['same','child','ancestor'])
def test_other_persistent_stores_cannot_overlap_either_direction(tmp_path,name,relationship):
    other=tmp_path/'evidence'
    root={'same':other,'child':other/'parameters','ancestor':tmp_path}[relationship]
    with pytest.raises(ApiError) as error:
        module.ParameterTemplates(settings(root,**{name:str(other)}))
    assert error.value.code=='parameter_templates_path'
    assert not other.exists()


def test_relative_store_cannot_resolve_inside_unknown_working_directory():
    with pytest.raises(ApiError) as error:module.ParameterTemplates(settings('relative-parameters'))
    assert error.value.code=='parameter_templates_path'
