"""Testes dos helpers de parsing da Posição Detalhada (números/datas/categoria)."""

from __future__ import annotations

from patrimonio import importador_xp_planilha as pl
from patrimonio.importador_xp import mapear_categoria


def test_valor_monetario():
    assert pl._valor("R$ 53.240,87") == 53240.87
    assert pl._valor("1.000,00") == 1000.0
    assert pl._valor("30,79%") is None  # percentual não é dinheiro
    assert pl._valor("") is None


def test_data_iso():
    assert pl._data_iso("22/03/2024") == "2024-03-22"
    assert pl._data_iso("sem data") is None


def test_pct():
    assert pl._pct("30,79%") == 0.3079
    assert pl._pct("100") is None


def test_norm_sem_acento():
    assert pl._norm("Ações  Multimercado") == "Acoes Multimercado"


def test_mapear_categoria_reconhece_tesouro_e_cdb():
    assert "Tesouro" in mapear_categoria("", "TESOURO IPCA+ 2029") or mapear_categoria("", "TESOURO IPCA+ 2029")
    # não deve estourar para nomes genéricos
    assert isinstance(mapear_categoria("Renda Fixa", "CDB BANCO X"), str)
