from __future__ import annotations

from datetime import UTC, datetime

import pytest

from amphour.line_protocol import to_line

TS = datetime(2026, 9, 9, 4, 6, 43, tzinfo=UTC)


def test_basic_point():
    assert to_line("reading", {"volts": 11.5}, timestamp=TS) == "reading volts=11.5 1788926803"


def test_integers_get_the_i_suffix():
    """Without it InfluxDB stores them as floats and the field type can conflict."""
    assert to_line("m", {"soc": 16}, timestamp=None) == "m soc=16i"


def test_booleans_are_not_serialised_as_integers():
    """bool is a subclass of int; the order of the isinstance checks matters."""
    assert to_line("m", {"ok": True}) == "m ok=t"
    assert to_line("m", {"ok": False}) == "m ok=f"


def test_strings_are_quoted_and_escaped():
    assert to_line("m", {"s": 'say "hi"'}) == 'm s="say \\"hi\\""'
    assert to_line("m", {"s": "back\\slash"}) == 'm s="back\\\\slash"'


def test_commas_and_spaces_are_escaped_in_keys_and_tags():
    line = to_line("odd name", {"field key": 1.0}, {"tag key": "tag,value"})
    assert line == "odd\\ name,tag\\ key=tag\\,value field\\ key=1.0"


def test_equals_is_escaped_in_tags_but_not_in_the_measurement():
    """The specification treats them differently; getting this backwards is a
    classic hand-rolled-writer bug."""
    assert to_line("a=b", {"f": 1.0}) == "a=b f=1.0"
    assert to_line("m", {"f": 1.0}, {"k": "a=b"}) == "m,k=a\\=b f=1.0"


def test_tags_are_sorted_by_key():
    line = to_line("m", {"f": 1.0}, {"zeta": "1", "alpha": "2"})
    assert line == "m,alpha=2,zeta=1 f=1.0"


def test_empty_tag_values_are_dropped():
    """InfluxDB rejects an empty tag value; dropping beats a 400."""
    assert to_line("m", {"f": 1.0}, {"host": "", "keep": "yes"}) == "m,keep=yes f=1.0"


def test_a_point_with_no_fields_is_an_error():
    with pytest.raises(ValueError, match="at least one field"):
        to_line("m", {})


@pytest.mark.parametrize(
    ("precision", "expected"),
    [("s", "1788926803"), ("ms", "1788926803000"), ("u", "1788926803000000")],
)
def test_precision_scales_the_timestamp(precision, expected):
    line = to_line("m", {"f": 1.0}, timestamp=TS, precision=precision)
    assert line.endswith(expected)


def test_unsupported_precision_is_rejected():
    with pytest.raises(ValueError, match="precision"):
        to_line("m", {"f": 1.0}, timestamp=TS, precision="fortnights")
