"""Testes da rentabilidade do extrato XP na análise (COE com cupom)."""

from __future__ import annotations

from unittest.mock import patch

from patrimonio import analise, database as db


@patch("patrimonio.analise.mercado.cdi_acumulado", return_value=0.320348)
@patch("patrimonio.analise.mercado.ipca_acumulado", return_value=0.12)
def test_coe_usa_rent_bruta_extrato_nao_posicao(_ipca, _cdi, banco_temporario):
    tit = db.id_titular("Lidiana")
    ativo_id = db.inserir_ativo(
        tit,
        "XP FS BIO 2031 RF Pre",
        "COE",
        data_aplicacao="2024-04-11",
        valor_aplicado=7500.0,
        rent_bruta_extrato=0.3079,
    )
    db.registrar_snapshot(ativo_id, "2026-07-01", 7910.0)
    row = db.obter_ativo(ativo_id)

    a = analise.analisar_ativo(row, hoje="2026-07-12")

    assert a.rent_fonte == "extrato"
    assert abs(a.rent_bruta - 0.3079) < 0.0001
    assert a.valor_atual == 7910.0
    assert abs(a.valor_economico - 9809.25) < 0.1
    assert abs(a.ganho_bruto - 2309.25) < 0.1
    # ~96% do CDI, não 17%
    assert a.pct_cdi is not None
    assert a.pct_cdi > 0.90
    assert a.pct_cdi < 1.0
    niveis = [al.nivel for al in a.alertas]
    assert "revisao" not in niveis


@patch("patrimonio.analise.mercado.cdi_acumulado", return_value=0.320348)
def test_sem_extrato_usa_posicao(_cdi, banco_temporario):
    tit = db.id_titular("Lidiana")
    ativo_id = db.inserir_ativo(
        tit, "COE sem extrato", "COE", valor_aplicado=7500.0, data_aplicacao="2024-04-11"
    )
    db.registrar_snapshot(ativo_id, "2026-07-01", 7910.0)
    row = db.obter_ativo(ativo_id)

    a = analise.analisar_ativo(row, hoje="2026-07-12")

    assert a.rent_fonte == "posicao"
    assert abs(a.rent_bruta - (7910 / 7500 - 1)) < 0.0001
    assert a.pct_cdi is not None
    assert a.pct_cdi < 0.20
