"""Testes das novas tabelas de fundos (cache de cotas, cadastro, universo)."""

from __future__ import annotations

from patrimonio import database as db


def test_cotas_fundo_upsert_idempotente(banco_temporario):
    cnpj = "12.345.678/0001-99"
    db.gravar_cotas_fundo(cnpj, [{"data": "2025-01-02", "cota": 1.5, "pl": 100.0, "cotistas": 3}])
    # regravar a mesma data não duplica e atualiza o valor
    db.gravar_cotas_fundo(cnpj, [{"data": "2025-01-02", "cota": 1.75}])
    serie = db.serie_cotas_fundo("12345678000199")
    assert len(serie) == 1
    assert serie[0]["cota"] == 1.75


def test_cota_fundo_em_e_recente(banco_temporario):
    cnpj = "12345678000199"
    db.gravar_cotas_fundo(cnpj, [
        {"data": "2025-01-02", "cota": 1.0},
        {"data": "2025-02-03", "cota": 1.2},
        {"data": "2025-03-04", "cota": 1.3},
    ])
    assert db.ultima_data_cota(cnpj) == "2025-03-04"
    assert db.cota_fundo_em(cnpj, "2025-02-15")["data"] == "2025-02-03"
    assert db.cota_fundo_recente(cnpj)["cota"] == 1.3
    # data anterior a toda a série → None
    assert db.cota_fundo_em(cnpj, "2024-12-31") is None


def test_pares_classe_e_melhores(banco_temporario):
    ref = "2026-06-30"
    for i, r in enumerate([0.05, 0.10, 0.15]):
        db.upsert_fundos_cadastro([{"cnpj": f"0000000000000{i}", "denominacao": f"F{i}", "classe": "RF"}])
        db.gravar_universo_retornos([{"cnpj": f"0000000000000{i}", "janela": 12, "data_ref": ref, "retorno": r}])
    db.gravar_pares_classe([{"classe": "RF", "janela": 12, "data_ref": ref, "mediana": 0.10, "q1": None, "q3": None, "n": 3}])
    par = db.obter_pares_classe("RF", 12)
    assert par["mediana"] == 0.10
    melhores = db.melhores_da_classe("RF", 12, 0.10, data_ref=ref)
    # retorno >= 0.10 → dois fundos (0.10 e 0.15)
    assert {m["cnpj"][-1] for m in melhores} == {"1", "2"}
