"""Only explicitly supplied unknown identity fields can be completed."""
import pytest
from pydantic import ValidationError

from app.schemas.setup import AssetIdentity


@pytest.mark.parametrize("data", [
    {}, {"serial_number": None}, {"model": None}, {"serial_number": " "},
    {"model": " "}, {"serial_number": "x" * 256}, {"model": "x" * 256},
    {"serial_number": "SN\x00"}, {"model": "24\x00轴"},
    {"serial_number": "SN", "notes": "cannot edit other metadata"},
])
def test_identity_completion_refuses_clear_empty_or_unrelated_fields(data):
    with pytest.raises(ValidationError):
        AssetIdentity.model_validate({"reason": "Synthetic completion", **data})


def test_identity_completion_preserves_missing_fields_and_trims_actual_values():
    value = AssetIdentity.model_validate({"serial_number": " SN-001 ", "reason": " verified completion "})
    assert value.serial_number == "SN-001" and value.model is None
    assert "model" not in value.model_fields_set
