"""Testes do parsing do Informe Diário da CVM (sem rede: ZIP sintético)."""

from __future__ import annotations

import io
import zipfile

import pytest

from patrimonio import cvm


def _zip_informe() -> bytes:
    """Monta um ZIP no formato do Informe Diário (CSV ';', latin-1)."""
    linhas = [
        "CNPJ_FUNDO_CLASSE;DT_COMPTC;VL_QUOTA;VL_PATRIM_LIQ;NR_COTST",
        "11111111111111;2026-06-01;10,00;1000000;100",
        "11111111111111;2026-06-15;11,00;1100000;110",
        "11111111111111;2026-06-30;12,00;1200000;120",
        "22222222222222;2026-06-30;5,00;500000;50",
    ]
    csv_bytes = "\n".join(linhas).encode("latin-1")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("inf_diario_fi_202606.csv", csv_bytes)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _mock_informe(monkeypatch):
    monkeypatch.setattr(cvm, "_baixar_informe", lambda aaaamm: _zip_informe())


def test_universo_cotas_mes_pega_ultimo_pregao_ate_limite():
    mp = cvm.universo_cotas_mes("202606", "2026-06-20")
    # fundo 1: último pregão <= 20/06 é 15/06 → cota 11,0
    assert mp["11111111111111"] == 11.0
    # fundo 2 só tem pregão em 30/06 → não entra até 20/06
    assert "22222222222222" not in mp


def test_universo_cotas_mes_limite_final():
    mp = cvm.universo_cotas_mes("202606", "2026-06-30")
    assert mp["11111111111111"] == 12.0
    assert mp["22222222222222"] == 5.0


def test_serie_cotas_fundo_com_pl_e_cotistas():
    serie = cvm.serie_cotas_fundo("11111111111111", "2026-06-01", "2026-06-30")
    assert [p["data"] for p in serie] == ["2026-06-01", "2026-06-15", "2026-06-30"]
    assert serie[-1]["cota"] == 12.0
    assert serie[-1]["pl"] == 1200000.0
    assert serie[-1]["cotistas"] == 120


def test_num_e_int_opt():
    assert cvm._num_opt("12,34") == 12.34
    assert cvm._num_opt("12.34") == 12.34
    assert cvm._num_opt("") is None
    assert cvm._int_opt("120") == 120
    assert cvm._int_opt("") is None
