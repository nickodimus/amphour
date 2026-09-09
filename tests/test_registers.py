from __future__ import annotations

from amphour.drivers.renogy.protocol import START_REGISTER, WORD_COUNT
from amphour.drivers.renogy.registers import BY_NAME, FIELDS, REGISTERS


def test_every_register_falls_inside_the_window_we_actually_request():
    last = START_REGISTER + WORD_COUNT
    for r in REGISTERS:
        assert START_REGISTER <= r.register < last, r.name
        if r.kind == "u32":
            assert r.register + 1 < last, f"{r.name} 32-bit read runs past the frame"


def test_field_names_are_unique():
    assert len(BY_NAME) == len(REGISTERS)


def test_unverified_fields_are_not_exported_by_default():
    """Nobody should be able to build an alarm on a hypothesis by accident."""
    from amphour.fields import exported

    default = exported(FIELDS)
    for f in FIELDS:
        if f.confidence == "unverified":
            assert f.name not in default, f.name


def test_every_field_documents_itself():
    for f in FIELDS:
        assert f.help.strip(), f.name
        assert f.unit.strip(), f.name


def test_the_eighteen_fields_the_old_app_had_are_all_confirmed():
    """Anything the predecessor shipped has been cross-checked against it."""
    from tests.conftest import OLD_TO_NEW

    for new_name in OLD_TO_NEW.values():
        assert BY_NAME[new_name].confidence == "confirmed", new_name
