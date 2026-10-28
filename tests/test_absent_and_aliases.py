"""Correcoes de 2026-10-28: instituicao ausente da pagina nunca vira normal nem "Nao monitorado";
aliases de Itau e Caixa conferidos com o HTML real exportado pelo usuario."""

import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bankmon import status
from bankmon.status import RunView, effective_state

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


def test_not_listed_is_no_data_never_normal_nor_not_monitored():
    run = RunView(1, NOW - timedelta(minutes=5), "ok", False, {"itau": "not_listed", "nubank": "no_alert"})
    estado = effective_state("itau", run, NOW, timedelta(minutes=90))
    assert estado == status.NO_DATA
    assert estado not in (status.NO_ALERT, status.NOT_MONITORED)
    assert effective_state("nubank", run, NOW, timedelta(minutes=90)) == status.NO_ALERT


def test_aliases_match_real_downdetector_names():
    cfg = tomllib.loads((ROOT / "config" / "institutions.toml").read_text(encoding="utf-8"))
    labels = {i["id"]: i["dd_label"] for i in cfg["institutions"]}
    assert labels["itau"] == "Banco Itaú"
    assert labels["caixa"] == "Caixa Econômica Federal"
