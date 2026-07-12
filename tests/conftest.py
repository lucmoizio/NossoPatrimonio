"""Fixtures compartilhadas dos testes (sem rede).

Aponta o banco padrão para um SQLite temporário, de modo que qualquer função
que use `database.CAMINHO_BANCO` (inclusive as chamadas internas de `cvm.py`)
grave num banco isolado por teste.
"""

from __future__ import annotations

import pytest

from patrimonio import database


@pytest.fixture(autouse=True)
def banco_temporario(tmp_path, monkeypatch):
    caminho = tmp_path / "teste.db"
    monkeypatch.setattr(database, "CAMINHO_BANCO", caminho)
    database.inicializar(caminho)
    yield caminho
