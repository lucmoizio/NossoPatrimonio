"""Persistência em SQLite local (sem ORM).

Todos os dados do usuário ficam em `patrimonio.db` na raiz do projeto —
privacidade local-first (nada de nuvem). As migrações são "leves": a função
`inicializar()` é idempotente e adiciona colunas faltantes verificando o
esquema atual via `PRAGMA table_info`.

Convenções:
    - datas em ISO `yyyy-mm-dd` (texto);
    - valores monetários em REAL (BRL);
    - categoria é texto de lista fechada definida na UI (`app.CATEGORIAS`).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Iterator, Optional

# Banco na raiz do projeto (um nível acima deste pacote).
CAMINHO_BANCO = Path(__file__).resolve().parent.parent / "patrimonio.db"

# Titulares fixos do sistema (casal).
TITULARES_PADRAO = ("Lidiana", "Luciano")


# --------------------------------------------------------------------------- #
# Conexão
# --------------------------------------------------------------------------- #
def conectar(caminho: Optional[Path] = None) -> sqlite3.Connection:
    """Abre uma conexão SQLite com row_factory por nome e FKs ativas."""
    con = sqlite3.connect(str(caminho or CAMINHO_BANCO))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


@contextmanager
def transacao(caminho: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """Context manager transacional: faz commit no sucesso e rollback no erro."""
    con = conectar(caminho)
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Migrações leves
# --------------------------------------------------------------------------- #
def _colunas(con: sqlite3.Connection, tabela: str) -> set[str]:
    """Retorna o conjunto de nomes de colunas existentes em `tabela`."""
    cur = con.execute(f"PRAGMA table_info({tabela})")
    return {linha["name"] for linha in cur.fetchall()}


def _garantir_coluna(
    con: sqlite3.Connection, tabela: str, coluna: str, definicao: str
) -> None:
    """Adiciona `coluna` a `tabela` caso ainda não exista (migração leve)."""
    if coluna not in _colunas(con, tabela):
        con.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {definicao}")


def inicializar(caminho: Optional[Path] = None) -> None:
    """Cria/atualiza o esquema e semeia os titulares fixos. Idempotente."""
    with transacao(caminho) as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS titulares (
                id   INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS ativos (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                titular_id     INTEGER NOT NULL REFERENCES titulares(id),
                nome           TEXT NOT NULL,
                categoria      TEXT NOT NULL,
                liquidez       TEXT,
                taxa_adm_aa    REAL,
                isento_ir      INTEGER NOT NULL DEFAULT 0,
                come_cotas     INTEGER NOT NULL DEFAULT 0,
                data_aplicacao TEXT,
                valor_aplicado REAL NOT NULL DEFAULT 0,
                observacoes    TEXT,
                ativo          INTEGER NOT NULL DEFAULT 1,
                cnpj           TEXT
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ativo_id INTEGER NOT NULL REFERENCES ativos(id) ON DELETE CASCADE,
                data     TEXT NOT NULL,
                valor    REAL NOT NULL,
                UNIQUE(ativo_id, data)
            );

            CREATE TABLE IF NOT EXISTS movimentos (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ativo_id INTEGER NOT NULL REFERENCES ativos(id) ON DELETE CASCADE,
                data     TEXT NOT NULL,
                tipo     TEXT NOT NULL CHECK (tipo IN ('aporte', 'resgate')),
                valor    REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS metas (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                descricao   TEXT NOT NULL,
                valor_alvo  REAL NOT NULL,
                prazo_anos  REAL NOT NULL,
                aporte_anual REAL NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS proventos (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ativo_id INTEGER NOT NULL REFERENCES ativos(id) ON DELETE CASCADE,
                data     TEXT NOT NULL,
                tipo     TEXT NOT NULL,
                valor    REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sim_config (
                id               INTEGER PRIMARY KEY CHECK (id = 1),
                saldo_inicial    REAL NOT NULL,
                data_inicio      TEXT NOT NULL,
                custo_pct        REAL NOT NULL DEFAULT 0.03,
                limite_perda_pct REAL NOT NULL DEFAULT 20.0
            );

            CREATE TABLE IF NOT EXISTS sim_ordens (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                data       TEXT NOT NULL,
                ticker     TEXT NOT NULL,
                tipo       TEXT NOT NULL CHECK (tipo IN ('compra', 'venda')),
                quantidade REAL NOT NULL,
                preco      REAL NOT NULL,
                custos     REAL NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_ativos_titular ON ativos(titular_id);
            CREATE INDEX IF NOT EXISTS idx_snapshots_ativo ON snapshots(ativo_id);
            CREATE INDEX IF NOT EXISTS idx_movimentos_ativo ON movimentos(ativo_id);
            CREATE INDEX IF NOT EXISTS idx_proventos_ativo ON proventos(ativo_id);
            """
        )

        # Migrações leves para bancos criados por versões anteriores.
        _garantir_coluna(con, "ativos", "cnpj", "TEXT")
        _garantir_coluna(con, "ativos", "come_cotas", "INTEGER NOT NULL DEFAULT 0")
        _garantir_coluna(con, "ativos", "observacoes", "TEXT")
        _garantir_coluna(con, "ativos", "ativo", "INTEGER NOT NULL DEFAULT 1")

        # Seed dos titulares fixos.
        for nome in TITULARES_PADRAO:
            con.execute(
                "INSERT OR IGNORE INTO titulares (nome) VALUES (?)", (nome,)
            )


# --------------------------------------------------------------------------- #
# Titulares
# --------------------------------------------------------------------------- #
def listar_titulares(caminho: Optional[Path] = None) -> list[sqlite3.Row]:
    with transacao(caminho) as con:
        return con.execute(
            "SELECT id, nome FROM titulares ORDER BY nome"
        ).fetchall()


def id_titular(nome: str, caminho: Optional[Path] = None) -> Optional[int]:
    with transacao(caminho) as con:
        linha = con.execute(
            "SELECT id FROM titulares WHERE nome = ?", (nome,)
        ).fetchone()
        return int(linha["id"]) if linha else None


# --------------------------------------------------------------------------- #
# Ativos
# --------------------------------------------------------------------------- #
def inserir_ativo(
    titular_id: int,
    nome: str,
    categoria: str,
    *,
    liquidez: Optional[str] = None,
    taxa_adm_aa: Optional[float] = None,
    isento_ir: bool = False,
    come_cotas: bool = False,
    data_aplicacao: Optional[str] = None,
    valor_aplicado: float = 0.0,
    observacoes: Optional[str] = None,
    cnpj: Optional[str] = None,
    caminho: Optional[Path] = None,
) -> int:
    """Cadastra um ativo e retorna seu id."""
    with transacao(caminho) as con:
        cur = con.execute(
            """
            INSERT INTO ativos (
                titular_id, nome, categoria, liquidez, taxa_adm_aa,
                isento_ir, come_cotas, data_aplicacao, valor_aplicado,
                observacoes, ativo, cnpj
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                titular_id,
                nome,
                categoria,
                liquidez,
                taxa_adm_aa,
                int(isento_ir),
                int(come_cotas),
                data_aplicacao,
                valor_aplicado,
                observacoes,
                cnpj,
            ),
        )
        return int(cur.lastrowid)


def listar_ativos(
    titular_id: Optional[int] = None,
    *,
    apenas_ativos: bool = True,
    caminho: Optional[Path] = None,
) -> list[sqlite3.Row]:
    """Lista ativos, opcionalmente filtrando por titular."""
    clausulas: list[str] = []
    params: list[Any] = []
    if apenas_ativos:
        clausulas.append("a.ativo = 1")
    if titular_id is not None:
        clausulas.append("a.titular_id = ?")
        params.append(titular_id)
    where = ("WHERE " + " AND ".join(clausulas)) if clausulas else ""
    with transacao(caminho) as con:
        return con.execute(
            f"""
            SELECT a.*, t.nome AS titular_nome
            FROM ativos a
            JOIN titulares t ON t.id = a.titular_id
            {where}
            ORDER BY t.nome, a.nome
            """,
            params,
        ).fetchall()


def obter_ativo(ativo_id: int, caminho: Optional[Path] = None) -> Optional[sqlite3.Row]:
    with transacao(caminho) as con:
        return con.execute(
            """
            SELECT a.*, t.nome AS titular_nome
            FROM ativos a JOIN titulares t ON t.id = a.titular_id
            WHERE a.id = ?
            """,
            (ativo_id,),
        ).fetchone()


def atualizar_ativo(ativo_id: int, campos: dict[str, Any], caminho: Optional[Path] = None) -> None:
    """Atualiza campos arbitrários (whitelist) de um ativo."""
    permitidos = {
        "nome", "categoria", "liquidez", "taxa_adm_aa", "isento_ir",
        "come_cotas", "data_aplicacao", "valor_aplicado", "observacoes",
        "ativo", "cnpj", "titular_id",
    }
    atualizacoes = {k: v for k, v in campos.items() if k in permitidos}
    if not atualizacoes:
        return
    sets = ", ".join(f"{k} = ?" for k in atualizacoes)
    params = list(atualizacoes.values()) + [ativo_id]
    with transacao(caminho) as con:
        con.execute(f"UPDATE ativos SET {sets} WHERE id = ?", params)


def remover_ativo(ativo_id: int, caminho: Optional[Path] = None) -> None:
    """Remove definitivamente o ativo e seus registros dependentes."""
    with transacao(caminho) as con:
        con.execute("DELETE FROM ativos WHERE id = ?", (ativo_id,))


def desativar_ativo(ativo_id: int, caminho: Optional[Path] = None) -> None:
    """Marca o ativo como inativo (preserva histórico)."""
    with transacao(caminho) as con:
        con.execute("UPDATE ativos SET ativo = 0 WHERE id = ?", (ativo_id,))


# --------------------------------------------------------------------------- #
# Snapshots (série temporal de valor bruto)
# --------------------------------------------------------------------------- #
def registrar_snapshot(
    ativo_id: int, data_ref: str, valor: float, caminho: Optional[Path] = None
) -> None:
    """Upsert do valor bruto do ativo na data (chave: ativo_id, data)."""
    with transacao(caminho) as con:
        con.execute(
            """
            INSERT INTO snapshots (ativo_id, data, valor)
            VALUES (?, ?, ?)
            ON CONFLICT(ativo_id, data) DO UPDATE SET valor = excluded.valor
            """,
            (ativo_id, data_ref, valor),
        )


def listar_snapshots(ativo_id: int, caminho: Optional[Path] = None) -> list[sqlite3.Row]:
    with transacao(caminho) as con:
        return con.execute(
            "SELECT data, valor FROM snapshots WHERE ativo_id = ? ORDER BY data",
            (ativo_id,),
        ).fetchall()


def ultimo_snapshot(ativo_id: int, caminho: Optional[Path] = None) -> Optional[sqlite3.Row]:
    with transacao(caminho) as con:
        return con.execute(
            "SELECT data, valor FROM snapshots WHERE ativo_id = ? "
            "ORDER BY data DESC LIMIT 1",
            (ativo_id,),
        ).fetchone()


def todos_snapshots(caminho: Optional[Path] = None) -> list[sqlite3.Row]:
    """Retorna todos os snapshots com metadados do ativo (para relatórios)."""
    with transacao(caminho) as con:
        return con.execute(
            """
            SELECT s.ativo_id, s.data, s.valor, a.nome AS ativo_nome,
                   a.titular_id, a.categoria
            FROM snapshots s JOIN ativos a ON a.id = s.ativo_id
            ORDER BY s.data
            """
        ).fetchall()


# --------------------------------------------------------------------------- #
# Movimentos (aportes/resgates) — ajustam valor_aplicado
# --------------------------------------------------------------------------- #
def registrar_movimento(
    ativo_id: int, data_ref: str, tipo: str, valor: float, caminho: Optional[Path] = None
) -> None:
    """Registra aporte/resgate e ajusta `ativos.valor_aplicado` no mesmo passo.

    Aporte soma ao custo aplicado; resgate subtrai (mínimo zero).
    """
    if tipo not in ("aporte", "resgate"):
        raise ValueError("tipo deve ser 'aporte' ou 'resgate'")
    with transacao(caminho) as con:
        con.execute(
            "INSERT INTO movimentos (ativo_id, data, tipo, valor) VALUES (?, ?, ?, ?)",
            (ativo_id, data_ref, tipo, valor),
        )
        delta = valor if tipo == "aporte" else -valor
        con.execute(
            "UPDATE ativos SET valor_aplicado = MAX(0, valor_aplicado + ?) WHERE id = ?",
            (delta, ativo_id),
        )


def listar_movimentos(ativo_id: int, caminho: Optional[Path] = None) -> list[sqlite3.Row]:
    with transacao(caminho) as con:
        return con.execute(
            "SELECT data, tipo, valor FROM movimentos WHERE ativo_id = ? ORDER BY data",
            (ativo_id,),
        ).fetchall()


# --------------------------------------------------------------------------- #
# Metas
# --------------------------------------------------------------------------- #
def salvar_meta(
    descricao: str,
    valor_alvo: float,
    prazo_anos: float,
    aporte_anual: float,
    caminho: Optional[Path] = None,
) -> int:
    with transacao(caminho) as con:
        cur = con.execute(
            "INSERT INTO metas (descricao, valor_alvo, prazo_anos, aporte_anual) "
            "VALUES (?, ?, ?, ?)",
            (descricao, valor_alvo, prazo_anos, aporte_anual),
        )
        return int(cur.lastrowid)


def listar_metas(caminho: Optional[Path] = None) -> list[sqlite3.Row]:
    with transacao(caminho) as con:
        return con.execute(
            "SELECT id, descricao, valor_alvo, prazo_anos, aporte_anual "
            "FROM metas ORDER BY id"
        ).fetchall()


def remover_meta(meta_id: int, caminho: Optional[Path] = None) -> None:
    with transacao(caminho) as con:
        con.execute("DELETE FROM metas WHERE id = ?", (meta_id,))


# --------------------------------------------------------------------------- #
# Proventos
# --------------------------------------------------------------------------- #
def registrar_provento(
    ativo_id: int, data_ref: str, tipo: str, valor: float, caminho: Optional[Path] = None
) -> int:
    with transacao(caminho) as con:
        cur = con.execute(
            "INSERT INTO proventos (ativo_id, data, tipo, valor) VALUES (?, ?, ?, ?)",
            (ativo_id, data_ref, tipo, valor),
        )
        return int(cur.lastrowid)


def listar_proventos(
    ano: Optional[int] = None, caminho: Optional[Path] = None
) -> list[sqlite3.Row]:
    where = "WHERE strftime('%Y', p.data) = ?" if ano is not None else ""
    params = [str(ano)] if ano is not None else []
    with transacao(caminho) as con:
        return con.execute(
            f"""
            SELECT p.id, p.ativo_id, p.data, p.tipo, p.valor,
                   a.nome AS ativo_nome, a.titular_id
            FROM proventos p JOIN ativos a ON a.id = p.ativo_id
            {where}
            ORDER BY p.data
            """,
            params,
        ).fetchall()


def remover_provento(provento_id: int, caminho: Optional[Path] = None) -> None:
    with transacao(caminho) as con:
        con.execute("DELETE FROM proventos WHERE id = ?", (provento_id,))


# --------------------------------------------------------------------------- #
# Simulador
# --------------------------------------------------------------------------- #
def obter_sim_config(caminho: Optional[Path] = None) -> Optional[sqlite3.Row]:
    with transacao(caminho) as con:
        return con.execute("SELECT * FROM sim_config WHERE id = 1").fetchone()


def salvar_sim_config(
    saldo_inicial: float,
    data_inicio: str,
    custo_pct: float = 0.03,
    limite_perda_pct: float = 20.0,
    caminho: Optional[Path] = None,
) -> None:
    with transacao(caminho) as con:
        con.execute(
            """
            INSERT INTO sim_config (id, saldo_inicial, data_inicio, custo_pct, limite_perda_pct)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                saldo_inicial = excluded.saldo_inicial,
                data_inicio = excluded.data_inicio,
                custo_pct = excluded.custo_pct,
                limite_perda_pct = excluded.limite_perda_pct
            """,
            (saldo_inicial, data_inicio, custo_pct, limite_perda_pct),
        )


def registrar_sim_ordem(
    data_ref: str,
    ticker: str,
    tipo: str,
    quantidade: float,
    preco: float,
    custos: float,
    caminho: Optional[Path] = None,
) -> int:
    with transacao(caminho) as con:
        cur = con.execute(
            """
            INSERT INTO sim_ordens (data, ticker, tipo, quantidade, preco, custos)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (data_ref, ticker, tipo, quantidade, preco, custos),
        )
        return int(cur.lastrowid)


def listar_sim_ordens(caminho: Optional[Path] = None) -> list[sqlite3.Row]:
    with transacao(caminho) as con:
        return con.execute(
            "SELECT id, data, ticker, tipo, quantidade, preco, custos "
            "FROM sim_ordens ORDER BY data, id"
        ).fetchall()


def limpar_simulador(caminho: Optional[Path] = None) -> None:
    """Zera ordens e configuração do simulador (recomeçar o jogo)."""
    with transacao(caminho) as con:
        con.execute("DELETE FROM sim_ordens")
        con.execute("DELETE FROM sim_config")


if __name__ == "__main__":  # smoke manual
    inicializar()
    print(f"Banco inicializado em {CAMINHO_BANCO}")
    print("Titulares:", [t["nome"] for t in listar_titulares()])
