"""Testes do reset da plataforma (zerar_dados)."""

from __future__ import annotations

import json

from patrimonio import cadastro_cvm, database as db


def test_zerar_limpa_carteira_e_motor(banco_temporario):
    tit = db.id_titular("Lidiana")
    ativo_id = db.inserir_ativo(
        tit,
        "FUNDO X",
        "Fundo Multimercado",
        cnpj="11111111111111",
        data_aplicacao="2024-04-12",
        rent_bruta_extrato=0.12,
    )
    db.registrar_snapshot(ativo_id, "2026-06-01", 1000.0)
    db.registrar_movimento(ativo_id, "2026-06-01", "aporte", 1000.0)
    db.registrar_provento(ativo_id, "2026-06-15", "juros", 50.0)
    db.gravar_extrato_conta(
        tit,
        [
            {
                "conta": "128199",
                "data_mov": "2024-04-12",
                "data_liq": "2024-04-12",
                "lancamento": "COMPRA COE XP5324DBIGB",
                "valor": -10000.0,
                "saldo": 500.0,
                "tipo": "compra",
                "ativo_nome": "COE XP5324DBIGB",
                "ativo_id": ativo_id,
                "import_hash": "hash_teste_zerar_1",
            }
        ],
    )
    db.gravar_cotas_fundo("11111111111111", [{"data": "2026-06-01", "cota": 1.0}])
    db.upsert_fundos_cadastro([{"cnpj": "11111111111111", "denominacao": "FUNDO X", "classe": "RF"}])
    db.gravar_universo_retornos(
        [{"cnpj": "11111111111111", "janela": 12, "data_ref": "2026-06-30", "retorno": 0.1}]
    )
    db.gravar_pares_classe(
        [{"classe": "RF", "janela": 12, "data_ref": "2026-06-30", "mediana": 0.08, "n": 10}]
    )
    db.salvar_meta("Meta teste", 1_000_000, 10, 50_000)

    removidos = db.zerar_dados(incluir_metas=True, incluir_simulador=False)

    assert removidos["extrato_conta"] == 1
    assert removidos["ativos"] == 1
    assert removidos["snapshots"] == 1
    assert removidos["movimentos"] == 1
    assert removidos["proventos"] == 1
    assert removidos["cotas_fundos"] == 1
    assert removidos["universo_retornos"] == 1
    assert removidos["pares_classe"] == 1
    assert removidos["metas"] == 1

    assert len(db.listar_ativos()) == 0
    assert db.contar_extrato_conta() == 0
    assert db.contar_fundos_cadastro() == 1  # cadastro oficial CVM preservado
    assert db.data_ref_universo() is None
    assert db.id_titular("Lidiana") is not None  # titulares preservados


def test_limpar_cache_validados(banco_temporario, monkeypatch, tmp_path):
    cache_dir = tmp_path / ".cache_cvm"
    cache_dir.mkdir()
    arq = cache_dir / "cnpj_validados.json"
    arq.write_text(
        json.dumps({"FUNDO X": {"cnpj": "11111111111111", "denominacao": "FUNDO X"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cadastro_cvm, "_DIR_CACHE", cache_dir)
    monkeypatch.setattr(cadastro_cvm, "_ARQ_VALIDADOS", arq)

    n = cadastro_cvm.limpar_cache_validados()
    assert n == 1
    assert not arq.exists()
