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


# --- SEC-007: redacao, ReDoS, Unicode ---------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "{'api_key': 'SEGREDO1'}",
        '{"Authorization": "Bearer SEGREDO1"}',
        "Authorization: Basic SEGREDO1abc=",
        "falha com Bearer SEGREDO1xyz no meio",
        "refresh_token=SEGREDO1",
        "client_secret=SEGREDO1",
        "auth_token=SEGREDO1",
        "https://user:SEGREDO1@x@host.example/p",
        "Cookie: sid=SEGREDO1; outro=SEGREDO2",
        "Set-Cookie: sid=SEGREDO1",
        "Basic dXNlcjpTRUdSRURPMQ==",
    ],
)
def test_secret_variants_do_not_leak(raw):
    out = safe_text(raw)
    assert "SEGREDO" not in out and "dXNlcjp" not in out


def test_plain_words_are_kept():
    out = safe_text("erro basic authentication ao consultar")
    assert out == "erro basic authentication ao consultar"


@pytest.mark.parametrize(
    "payload", ["a" * 100_000, "http://" * 20_000, "token=" * 20_000, "a@" * 50_000, "x" * 50_000 + "://"]
)
def test_large_input_is_fast(payload):
    import time

    t0 = time.perf_counter()
    out = safe_text(payload)
    assert time.perf_counter() - t0 < 0.5
    assert len(out) <= MAX_LEN


def test_unicode_invisibles_removed():
    out = safe_text("ok‮gnp.exe​  fim\u0085")
    for ch in ("‮", "​", " ", " ", "\u0085"):
        assert ch not in out
    assert out.startswith("okgnp.exe")


def test_runner_log_does_not_leak_secret(cfg, caplog):
    from bankmon import runner
    from conftest import FakeCollector, T0

    class Boom(FakeCollector):
        def collect(self, institutions):
            raise RuntimeError("falhou token=SEGREDO1 https://u:SEGREDO2@h/x?k=SEGREDO3")

    with caplog.at_level("DEBUG", logger="bankmon.runner"):
        runner.run_collection(cfg, "cli", now=T0, collector=Boom())
    assert "SEGREDO" not in caplog.text
    assert "Coletor fake falhou" in caplog.text


@pytest.mark.parametrize(
    "raw",
    [
        "auth=abc123xyz",
        "pass=abc123xyz",
        "credentials: abc123xyz",
        "db_credential=abc123xyz",
        "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abc123xyz",
    ],
)
def test_more_secret_formats_masked(raw):
    assert "abc123xyz" not in safe_text(f"erro {raw} fim")


def test_truncation_drops_partial_credential_token():
    # A query longa e removida, mas o corte em MAX_INPUT cai no meio do userinfo.
    raw = "x " * 985 + "https://user:SEKRETSEKRET" + "Z" * 50 + "@host/p"
    out = safe_text(raw, max_len=5000)
    assert "SEKRET" not in out
