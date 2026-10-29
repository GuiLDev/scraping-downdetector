"""Validacao estrita da geometria SVG (funcoes puras)."""

import pytest

from bankmon.collectors import svg_geometry as g

D_OK = "M0,10.5 L10 20 H30 V5 C1 2 3 4 5 6 S1 2 3 4 Q1 2 3 4 T5 6 A1 1 0 0 1 2 2 Z"


def test_viewbox_valid_is_normalized():
    assert g.validate_viewbox("0 0 100 40") == "0 0 100 40"
    assert g.validate_viewbox(" 0,0, 1e2 .5 ") == "0 0 1e2 .5"
    assert g.validate_viewbox("-5 -5 10000 10000") == "-5 -5 10000 10000"


@pytest.mark.parametrize("value", [
    None, "", "0 0 100", "0 0 100 40 5", "0 0 0 40", "0 0 100 -1", "0 0 10001 40",
    "0 0 nan 40", "0 0 inf 40", "0 0 1e999 40", "0 0 <b> 40", '0 0 "100" 40', "0 0 100 40;",
    "0 0 100 40 script", "url(#x) 0 100 40",
])
def test_viewbox_invalid(value):
    assert g.validate_viewbox(value) is None


def test_dimension():
    assert g.validate_dimension("100") == "100"
    assert g.validate_dimension("40px") == "40"
    assert g.validate_dimension(" 12.5PX ") == "12.5"
    assert g.validate_dimension("0") == "0"
    for bad in (None, "", "-1", "10001", "nan", "inf", "1e999", "10%", "auto", "1 2", "<1>", "url(x)"):
        assert g.validate_dimension(bad) is None


def test_path_d_valid_collapses_spaces():
    assert g.validate_path_d(D_OK) == D_OK
    assert g.validate_path_d("M0   0\n L1\t1") == "M0 0 L1 1"
    assert g.validate_path_d("m1e2 -3.5e-1 z") == "m1e2 -3.5e-1 z"
    assert g.validate_path_d("M.5.5L1-1") == "M.5.5L1-1"


@pytest.mark.parametrize("value", [
    None, "", "   ", "L0 0", "0 0 L1 1", "M0 0 <script>", 'M0 0 "', "M0 0 '", "M0 0 script",
    "M0 0 url(#a)", "M0 0 L1e999 2", "M0 0 L nan 1", "M0 0 L inf 1", "M0 0 L1 1;", "M0 0 X1 1",
    "M0 0 L1e", "M0 0 L- 1", "M0 0 &lt;", "M0 0 L1 1 /", "M0 0 L1 1 )",
])
def test_path_d_invalid(value):
    assert g.validate_path_d(value) is None


def test_path_d_length_limit():
    ok = "M0 0 " + "1 " * 9990
    assert len(ok) <= g.MAX_PATH_LEN and g.validate_path_d(ok) is not None
    assert g.validate_path_d("M0 0 " + "1 " * 10000) is None
