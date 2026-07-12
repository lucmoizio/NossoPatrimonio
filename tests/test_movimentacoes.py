"""Testes do parser do extrato de movimentações da XP (datas de aplicação)."""

from __future__ import annotations

import pytest

from patrimonio import importador_movimentacoes_xp as mov
from patrimonio.importador_xp import ErroImportacaoXP

CSV_MOV = (
    b"Data;Produto;Movimentacao;Quantidade;Valor\n"
    b"15/03/2023;XP MACRO PLUS FIC FIM;Aplicacao;100,00;R$ 10.000,00\n"
    b"20/07/2023;XP MACRO PLUS FIC FIM;Aplicacao;50,00;R$ 5.500,00\n"
    b"10/01/2024;XP MACRO PLUS FIC FIM;Resgate;-30,00;R$ 3.500,00\n"
    b"02/09/2022;TESOURO IPCA+ 2029;Compra;2,00;R$ 4.000,00\n"
)


def test_extrai_primeira_aplicacao_ignora_resgate():
    aplic = mov.extrair_aplicacoes(CSV_MOV, "movimentacoes.csv")
    por_nome = {a.nome: a for a in aplic}
    # pega a MENOR data de entrada (ignora o resgate de 2024)
    xp = next(a for a in aplic if "MACRO" in a.nome)
    assert xp.data == "2023-03-15"
    assert xp.ocorrencias == 2  # duas aplicações, resgate não conta


def test_casa_com_ativos_por_similaridade():
    aplic = mov.extrair_aplicacoes(CSV_MOV, "movimentacoes.csv")
    ativos = [
        {"id": 1, "nome": "XP Macro Plus FIC FIM", "data_aplicacao": None},
        {"id": 2, "nome": "Tesouro IPCA+ 2029", "data_aplicacao": "2026-07-11"},
    ]
    props = mov.casar_com_ativos(aplic, ativos)
    por_id = {p["id"]: p for p in props}
    assert por_id[1]["data_detectada"] == "2023-03-15"
    assert por_id[1]["score"] >= 0.9
    assert por_id[2]["data_detectada"] == "2022-09-02"


def test_arquivo_sem_aplicacoes_erra():
    csv_vazio = b"Data;Produto;Movimentacao\n01/01/2024;FUNDO X;Resgate\n"
    with pytest.raises(ErroImportacaoXP):
        mov.extrair_aplicacoes(csv_vazio, "x.csv")
