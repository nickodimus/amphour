from __future__ import annotations

from amphour.protocol import START_REGISTER, WORD_COUNT
from amphour.registers import BY_NAME, DEFAULT_EXPORTED, FIELDS


def test_every_register_falls_inside_the_window_we_actually_request():
    last = START_REGISTER + WORD_COUNT
    for f in FIELDS:
        assert START_REGISTER <= f.register < last, f.name
        if f.kind == "u32":
            assert f.register + 1 < last, f"{f.name} 32-bit read runs past the frame"


def test_field_names_are_unique():
    assert len(BY_NAME) == len(FIELDS)


def test_unverified_fields_are_not_exported_by_default():
    """Nobody should be able to build an alarm on a hypothesis by accident."""
    for f in FIELDS:
        if f.confidence == "unverified":
            assert f.name not in DEFAULT_EXPORTED, f.name


def test_every_field_documents_itself():
    for f in FIELDS:
        assert f.help.strip(), f.name
        assert f.unit.strip(), f.name


def test_the_eighteen_fields_the_old_app_had_are_all_confirmed():
    """Anything the predecessor shipped has been cross-checked against it."""
    from tests.conftest import OLD_TO_NEW

    for new_name in OLD_TO_NEW.values():
        assert BY_NAME[new_name].confidence == "confirmed", new_name
