import pytest
from pydantic import ValidationError
from app import Item


def test_item_input():
    assert Item(label='example').label == 'example'
    with pytest.raises(ValidationError):
        Item(label='')
