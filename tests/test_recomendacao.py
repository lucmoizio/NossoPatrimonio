"""Testes da lógica de comparação/recomendação (sem rede)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from patrimonio import database as db
from patrimonio import recomendacao as rec


def test_subtrai_meses():
    assert rec._subtrai_meses(date(2026, 6, 30), 12) == date(2025, 6, 30)
    assert rec._subtrai_meses(date(2026, 6, 30), 24) == date(2024, 6, 30)
    # ajuste de fim de mês (31/03 - 1 mês -> 28/02)
    assert rec._subtrai_meses(date(2026, 3, 31), 1) == date(2026, 2, 28)


def _montar_universo(ref="2026-06-30", classe="RF"):
    # 4 fundos peers com retornos crescentes em 3 janelas
    dados = {
        "0": [0.02, 0.05, 0.08],
        "1": [0.03, 0.07, 0.12],
        "2": [0.05, 0.10, 0.16],
        "3": [0.06, 0.11, 0.18],
    }
    for i, (r6, r12, r24) in dados.items():
        cnpj = f"1111111111111{i}"
        db.upsert_fundos_cadastro([{"cnpj": cnpj, "denominacao": f"PEER {i}", "classe": classe}])
        db.gravar_universo_retornos([
            {"cnpj": cnpj, "janela": 6, "data_ref": ref, "retorno": r6},
            {"cnpj": cnpj, "janela": 12, "data_ref": ref, "retorno": r12},
            {"cnpj": cnpj, "janela": 24, "data_ref": ref, "retorno": r24},
        ])
    db.gravar_pares_classe([
        {"classe": classe, "janela": 6, "data_ref": ref, "mediana": 0.04, "q1": None, "q3": None, "n": 4},
        {"classe": classe, "janela": 12, "data_ref": ref, "mediana": 0.085, "q1": None, "q3": None, "n": 4},
        {"classe": classe, "janela": 24, "data_ref": ref, "mediana": 0.14, "q1": None, "q3": None, "n": 4},
    ])


def test_sugerir_alternativas_exige_2_de_3_janelas():
    _montar_universo()
    alts = rec.sugerir_alternativas("RF", taxa_atual=None, data_ref="2026-06-30")
    cnpjs = [a.cnpj[-1] for a in alts]
    # peers 2 e 3 estão acima da mediana nas 3 janelas; peer 0 fica abaixo
    assert "2" in cnpjs and "3" in cnpjs
    assert "0" not in cnpjs
    assert all(a.janelas_acima >= 2 for a in alts)


def test_avaliar_carteira_sinaliza_e_sugere(monkeypatch):
    ref = "2026-06-30"
    _montar_universo(ref)  # medianas: 6m=0.04, 12m=0.085, 24m=0.14

    # Fundo da carteira rende ABAIXO da mediana em 6m e 12m (e acima em 24m):
    #   6m: 12/11.8812-1 ≈ 0.01  | 12m: 12/11.7647-1 ≈ 0.02 | 24m: 12/10-1 = 0.20
    cnpj = "99999999999999"
    db.upsert_fundos_cadastro([{"cnpj": cnpj, "denominacao": "MEU FUNDO", "classe": "RF"}])
    db.gravar_cotas_fundo(cnpj, [
        {"data": "2024-06-30", "cota": 10.0},
        {"data": "2025-06-30", "cota": 11.7647},
        {"data": "2025-12-30", "cota": 11.8812},
        {"data": ref, "cota": 12.0},
    ])
    tit = db.id_titular("Lidiana")
    db.inserir_ativo(tit, "MEU FUNDO", "Fundo Multimercado", cnpj=cnpj, valor_aplicado=1000.0)

    @dataclass
    class _Ind:
        cdi_aa = 13.0

    monkeypatch.setattr(rec.mercado, "indicadores_atuais", lambda: _Ind())

    meu = next(a for a in rec.avaliar_carteira(tit) if a.cnpj == cnpj)
    assert meu.baixo_desempenho is True  # abaixo em 6m e 12m (2 de 3)
    assert meu.alternativas  # peers 2 e 3 estão acima da mediana nas 3 janelas
    assert all(alt.cnpj != cnpj for alt in meu.alternativas)
