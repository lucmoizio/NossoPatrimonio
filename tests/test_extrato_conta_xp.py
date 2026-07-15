"""Testes do parser do Extrato da conta investimento da XP."""

from __future__ import annotations

import io

import pandas as pd
import pytest

from patrimonio import database as db
from patrimonio import importador_extrato_conta_xp as extrato
from patrimonio import relatorios
from patrimonio.importador_xp import ErroImportacaoXP


def _montar_xlsx_fixture() -> bytes:
    """Planilha sintética no layout do Extrato da conta XP."""
    rows = [
        ["", "", "", "", "", "", "Extrato da conta"],
        ["", "", "", "", "", "", "Data da consulta: 13/07/2026 20:53"],
        ["", "", "", "", "", "", "De: 01/01/2020 Até: 13/07/2026"],
        ["", "LIDIANA TESTE", "", "", "", "", "Conta XP: 999001"],
        ["", "", "", "", "", "", ""],
        ["", "Movimentação", "Liquidação", "Lançamento", "", "Valor (R$)", "Saldo (R$)"],
        [
            "",
            "12/04/2024",
            "12/04/2024",
            "COMPRA COE XP5324DBIGB",
            "",
            -50000.0,
            1000.0,
        ],
        [
            "",
            "15/08/2024",
            "15/08/2024",
            "Pgto Juros XP5324DBIGB | COE BANCO XP S.A. - FEV/2031",
            "",
            2500.0,
            3500.0,
        ],
        [
            "",
            "18/08/2022",
            "18/08/2022",
            "TED TER BCO 33 AGE 1 CTA 1 - TED APLICAÇÃO FUNDOS Persevera Yield FIRF L",
            "",
            -10000.0,
            500.0,
        ],
        [
            "",
            "20/03/2023",
            "20/03/2023",
            "TED TER BCO 33 AGE 1 CTA 1 - TED APLICAÇÃO FUNDOS Persevera Yield FIRF L",
            "",
            -5000.0,
            200.0,
        ],
        [
            "",
            "10/01/2025",
            "10/01/2025",
            "RESGATE COE XP5324DBIGB",
            "",
            30000.0,
            30200.0,
        ],
    ]
    df = pd.DataFrame(rows)
    buf = io.BytesIO()
    df.to_excel(buf, index=False, header=False, sheet_name="Planilha1")
    return buf.getvalue()


@pytest.fixture
def xlsx_extrato() -> bytes:
    return _montar_xlsx_fixture()


def test_eh_extrato_conta(xlsx_extrato):
    assert extrato.eh_extrato_conta(xlsx_extrato, "extrato.xlsx") is True


def test_importar_classifica_linhas(xlsx_extrato):
    r = extrato.importar(xlsx_extrato, "extrato.xlsx")
    assert r.metadados.conta == "999001"
    assert r.metadados.titular == "LIDIANA TESTE"
    assert r.metadados.periodo_real_de == "2022-08-18"
    assert r.metadados.periodo_real_ate == "2025-01-10"
    tipos = {ln.tipo for ln in r.linhas}
    assert "compra" in tipos
    assert "juros" in tipos
    assert "aplic_fundo" in tipos
    assert "resgate" in tipos


def test_primeira_aplicacao_menor_data(xlsx_extrato):
    r = extrato.importar(xlsx_extrato, "extrato.xlsx")
    aplic = r.primeira_aplicacao_por_ativo
    fundo = next(a for a in aplic if "Persevera" in a.nome)
    assert fundo.data == "2022-08-18"
    assert fundo.ocorrencias == 2
    coe = next(a for a in aplic if "XP5324" in a.nome)
    assert coe.data == "2024-04-12"


def test_casar_com_ativos_fundo_truncado(xlsx_extrato):
    r = extrato.importar(xlsx_extrato, "extrato.xlsx")
    ativos = [
        {
            "id": 1,
            "nome": "Persevera Yield FIRF LP",
            "data_aplicacao": None,
        },
        {
            "id": 2,
            "nome": "COE XP5324DBIGB FS BIO 2031",
            "data_aplicacao": None,
        },
    ]
    props = extrato.casar_aplicacoes_com_ativos(r.primeira_aplicacao_por_ativo, ativos)
    por_id = {p["id"]: p for p in props}
    assert por_id[1]["data_detectada"] == "2022-08-18"
    assert por_id[2]["data_detectada"] == "2024-04-12"


def test_proventos_propostos(xlsx_extrato):
    r = extrato.importar(xlsx_extrato, "extrato.xlsx")
    prov = r.proventos_propostos
    assert len(prov) == 1
    assert prov[0].valor == 2500.0
    assert prov[0].codigo == "XP5324DBIGB"


def test_import_hash_idempotente(xlsx_extrato):
    r = extrato.importar(xlsx_extrato, "extrato.xlsx")
    h1 = r.linhas[0].import_hash("999001")
    h2 = r.linhas[0].import_hash("999001")
    assert h1 == h2
    assert len(h1) == 32


def test_gravar_extrato_conta_idempotente(banco_temporario, xlsx_extrato):
    tit = db.id_titular("Lidiana")
    r = extrato.importar(xlsx_extrato, "extrato.xlsx")
    payload = [
        {
            "conta": r.metadados.conta,
            "data_mov": ln.data_mov,
            "data_liq": ln.data_liq,
            "lancamento": ln.lancamento,
            "valor": ln.valor,
            "saldo": ln.saldo,
            "tipo": ln.tipo,
            "ativo_nome": ln.ativo_nome,
            "ativo_id": None,
            "import_hash": ln.import_hash(r.metadados.conta),
        }
        for ln in r.linhas
    ]
    r1 = db.gravar_extrato_conta(tit, payload)
    r2 = db.gravar_extrato_conta(tit, payload)
    assert r1["inseridos"] == len(r.linhas)
    assert r2["ignorados"] == len(r.linhas)
    assert db.contar_extrato_conta(titular_id=tit) == len(r.linhas)


def test_relatorios_extrato(banco_temporario, xlsx_extrato):
    tit = db.id_titular("Lidiana")
    r = extrato.importar(xlsx_extrato, "extrato.xlsx")
    payload = [
        {
            "conta": r.metadados.conta,
            "data_mov": ln.data_mov,
            "data_liq": ln.data_liq,
            "lancamento": ln.lancamento,
            "valor": ln.valor,
            "saldo": ln.saldo,
            "tipo": ln.tipo,
            "ativo_nome": ln.ativo_nome,
            "ativo_id": None,
            "import_hash": ln.import_hash(r.metadados.conta),
        }
        for ln in r.linhas
    ]
    db.gravar_extrato_conta(tit, payload)
    saldo = relatorios.saldo_conta_extrato(titular_id=tit)
    assert not saldo.empty
    fluxo = relatorios.fluxo_mensal_extrato(titular_id=tit)
    assert fluxo["entradas"].sum() > 0


def test_zerar_limpa_extrato_conta(banco_temporario, xlsx_extrato):
    tit = db.id_titular("Lidiana")
    r = extrato.importar(xlsx_extrato, "extrato.xlsx")
    db.gravar_extrato_conta(
        tit,
        [
            {
                "conta": r.metadados.conta,
                "data_mov": r.linhas[0].data_mov,
                "data_liq": r.linhas[0].data_liq,
                "lancamento": r.linhas[0].lancamento,
                "valor": r.linhas[0].valor,
                "saldo": r.linhas[0].saldo,
                "tipo": r.linhas[0].tipo,
                "ativo_nome": r.linhas[0].ativo_nome,
                "ativo_id": None,
                "import_hash": r.linhas[0].import_hash(r.metadados.conta),
            }
        ],
    )
    removidos = db.zerar_dados()
    assert removidos.get("extrato_conta", 0) == 1


def test_arquivo_invalido_erra():
    buf = io.BytesIO()
    pd.DataFrame([["foo", "bar"]]).to_excel(buf, index=False, header=False)
    with pytest.raises(ErroImportacaoXP):
        extrato.importar(buf.getvalue(), "x.xlsx")
