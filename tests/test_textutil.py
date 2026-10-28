import pytest

from bankmon.textutil import MAX_LEN, safe_error, safe_text


def test_url_userinfo_and_query_removed():
    out = safe_text("GET https://user:pass@host.example/path?token=abc&x=1 falhou")
    assert "pass" not in out and "abc" not in out and "user" not in out
    assert "https://host.example/path" in out


@pytest.mark.parametrize("raw", ["token=abc123", "api_key: abc123", "password=abc123", "cf_clearance=abc123"])
def test_secret_pairs_masked(raw):
    assert "abc123" not in safe_text(f"erro {raw} fim")


def test_ansi_and_control_chars_removed():
    out = safe_text("\x1b[31mvermelho\x1b[0m\nlinha\x00dois\r\x07")
    assert "\x1b" not in out and "\n" not in out and "\x00" not in out and "\x07" not in out
    assert "vermelho" in out and "linha" in out


def test_truncated():
    out = safe_text("a" * 1000)
    assert len(out) == MAX_LEN and out.endswith("…")


def test_safe_error_includes_type_and_sanitizes():
    out = safe_error(RuntimeError("falha em http://u:p@h/x?secret=1"))
    assert out.startswith("RuntimeError: ") and "secret" not in out and "u:p" not in out


def test_collector_exception_detail_is_sanitized_in_db(cfg):
    from bankmon import runner
    from conftest import FakeCollector, T0

    class Boom(FakeCollector):
        def collect(self, institutions):
            raise RuntimeError("falhou https://u:p@h/x?token=zzz \x1b[31m")

    out = runner.run_collection(cfg, "cli", now=T0, collector=Boom())
    assert out.result.source_status == "error"
    assert "zzz" not in out.result.error_detail and "u:p" not in out.result.error_detail
    assert "\x1b" not in out.result.error_detail
