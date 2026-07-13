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

            -- Cache de cotas de fundos parseadas do Informe Diário da CVM.
            -- Evita reprocessar o CSV mensal (grande) a cada consulta e serve de
            -- base idempotente para o motor de atualização e de recomendação.
            CREATE TABLE IF NOT EXISTS cotas_fundos (
                cnpj     TEXT NOT NULL,
                data     TEXT NOT NULL,
                cota     REAL NOT NULL,
                pl       REAL,
                cotistas INTEGER,
                fonte    TEXT NOT NULL DEFAULT 'CVM',
                PRIMARY KEY (cnpj, data)
            );

            -- Cadastro de classes/fundos da CVM (classe, tipo e taxa de adm) para
            -- comparação por pares (mediana da classe).
            CREATE TABLE IF NOT EXISTS fundos_cadastro (
                cnpj         TEXT PRIMARY KEY,
                denominacao  TEXT,
                classe       TEXT,
                tipo         TEXT,
                taxa_adm     REAL,
                situacao     TEXT,
                atualizado_em TEXT
            );

            -- Retorno por janela (6/12/24m) de cada fundo do universo, numa data
            -- de referência. Base para estatísticas de classe e sugestões.
            CREATE TABLE IF NOT EXISTS universo_retornos (
                cnpj     TEXT NOT NULL,
                janela   INTEGER NOT NULL,
                data_ref TEXT NOT NULL,
                retorno  REAL NOT NULL,
                PRIMARY KEY (cnpj, janela, data_ref)
            );

            -- Estatísticas por classe/janela (mediana e quartis) numa data de ref.
            CREATE TABLE IF NOT EXISTS pares_classe (
                classe   TEXT NOT NULL,
                janela   INTEGER NOT NULL,
                data_ref TEXT NOT NULL,
                mediana  REAL NOT NULL,
                q1       REAL,
                q3       REAL,
                n        INTEGER NOT NULL,
                PRIMARY KEY (classe, janela, data_ref)
            );

            CREATE INDEX IF NOT EXISTS idx_ativos_titular ON ativos(titular_id);
            CREATE INDEX IF NOT EXISTS idx_snapshots_ativo ON snapshots(ativo_id);
            CREATE INDEX IF NOT EXISTS idx_movimentos_ativo ON movimentos(ativo_id);
            CREATE INDEX IF NOT EXISTS idx_proventos_ativo ON proventos(ativo_id);
            CREATE INDEX IF NOT EXISTS idx_cotas_fundos_cnpj ON cotas_fundos(cnpj);
            CREATE INDEX IF NOT EXISTS idx_universo_classe ON universo_retornos(cnpj);
            """
        )

        # Migrações leves para bancos criados por versões anteriores.
        _garantir_coluna(con, "ativos", "cnpj", "TEXT")
        _garantir_coluna(con, "ativos", "come_cotas", "INTEGER NOT NULL DEFAULT 0")
        _garantir_coluna(con, "ativos", "observacoes", "TEXT")
        _garantir_coluna(con, "ativos", "ativo", "INTEGER NOT NULL DEFAULT 1")
        # Rentabilidade total do extrato XP (inclui cupons pagos fora da posição).
        _garantir_coluna(con, "ativos", "rent_bruta_extrato", "REAL")

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
# Reset (zerar dados)
# --------------------------------------------------------------------------- #
# Tabelas apagadas pelo "Zerar dados". Ao adicionar persistência nova ligada à
# carteira ou ao motor de fundos, inclua a tabela aqui — senão o reset fica incompleto.
# `fundos_cadastro` fica de fora: é cadastro oficial da CVM (referência), não dado
# do usuário; rebaixá-lo forçaria re-sync pesado sem benefício para reimportar.
_TABELAS_ZERAR_CARTEIRA = ("proventos", "movimentos", "snapshots", "ativos")
_TABELAS_ZERAR_MOTOR = ("cotas_fundos", "universo_retornos", "pares_classe")
_TABELAS_ZERAR_METAS = ("metas",)
_TABELAS_ZERAR_SIMULADOR = ("sim_ordens", "sim_config")


def zerar_dados(
    caminho: Optional[Path] = None,
    *,
    incluir_metas: bool = True,
    incluir_simulador: bool = True,
) -> dict[str, int]:
    """Apaga os dados do usuário e retorna quantas linhas foram removidas.

    Sempre limpa carteira (ativos, snapshots, movimentos, proventos) e o cache
    do motor de fundos (cotas sincronizadas, universo de pares). Metas e
    simulador são opcionais. Os titulares fixos são preservados (e regarantidos).
    Operação irreversível — a UI exige confirmação explícita.
    """
    tabelas = list(_TABELAS_ZERAR_CARTEIRA) + list(_TABELAS_ZERAR_MOTOR)
    if incluir_metas:
        tabelas.extend(_TABELAS_ZERAR_METAS)
    if incluir_simulador:
        tabelas.extend(_TABELAS_ZERAR_SIMULADOR)

    removidos: dict[str, int] = {}
    with transacao(caminho) as con:
        for tabela in tabelas:
            n = con.execute(f"SELECT COUNT(*) AS c FROM {tabela}").fetchone()["c"]
            con.execute(f"DELETE FROM {tabela}")
            removidos[tabela] = int(n)
        # Regarante os titulares fixos (caso algum tenha sido removido).
        for nome in TITULARES_PADRAO:
            con.execute("INSERT OR IGNORE INTO titulares (nome) VALUES (?)", (nome,))
    return removidos


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
    rent_bruta_extrato: Optional[float] = None,
    caminho: Optional[Path] = None,
) -> int:
    """Cadastra um ativo e retorna seu id."""
    with transacao(caminho) as con:
        cur = con.execute(
            """
            INSERT INTO ativos (
                titular_id, nome, categoria, liquidez, taxa_adm_aa,
                isento_ir, come_cotas, data_aplicacao, valor_aplicado,
                observacoes, ativo, cnpj, rent_bruta_extrato
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
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
                rent_bruta_extrato,
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
        "ativo", "cnpj", "titular_id", "rent_bruta_extrato",
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


# --------------------------------------------------------------------------- #
# Cotas de fundos (cache do Informe Diário da CVM)
# --------------------------------------------------------------------------- #
def gravar_cotas_fundo(
    cnpj: str,
    cotas: list[dict],
    fonte: str = "CVM",
    caminho: Optional[Path] = None,
) -> int:
    """Upsert idempotente de cotas de um fundo.

    `cotas` é uma lista de dicts com chaves 'data' (ISO), 'cota' e, opcionalmente,
    'pl' e 'cotistas'. Retorna o número de linhas processadas.
    """
    cnpj_norm = "".join(ch for ch in str(cnpj) if ch.isdigit())
    if not cnpj_norm or not cotas:
        return 0
    with transacao(caminho) as con:
        con.executemany(
            """
            INSERT INTO cotas_fundos (cnpj, data, cota, pl, cotistas, fonte)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(cnpj, data) DO UPDATE SET
                cota = excluded.cota,
                pl = excluded.pl,
                cotistas = excluded.cotistas,
                fonte = excluded.fonte
            """,
            [
                (
                    cnpj_norm,
                    c["data"],
                    float(c["cota"]),
                    c.get("pl"),
                    c.get("cotistas"),
                    fonte,
                )
                for c in cotas
                if c.get("data") and c.get("cota") is not None
            ],
        )
    return len(cotas)


def ultima_data_cota(cnpj: str, caminho: Optional[Path] = None) -> Optional[str]:
    """Data (ISO) da cota mais recente já armazenada para o fundo, ou None."""
    cnpj_norm = "".join(ch for ch in str(cnpj) if ch.isdigit())
    with transacao(caminho) as con:
        linha = con.execute(
            "SELECT MAX(data) AS d FROM cotas_fundos WHERE cnpj = ?", (cnpj_norm,)
        ).fetchone()
        return linha["d"] if linha and linha["d"] else None


def cota_fundo_em(
    cnpj: str, data_max: str, caminho: Optional[Path] = None
) -> Optional[sqlite3.Row]:
    """Cota armazenada no pregão <= `data_max` (ISO). Retorna Row(data, cota) ou None."""
    cnpj_norm = "".join(ch for ch in str(cnpj) if ch.isdigit())
    with transacao(caminho) as con:
        return con.execute(
            "SELECT data, cota FROM cotas_fundos WHERE cnpj = ? AND data <= ? "
            "ORDER BY data DESC LIMIT 1",
            (cnpj_norm, data_max),
        ).fetchone()


def cota_fundo_recente(cnpj: str, caminho: Optional[Path] = None) -> Optional[sqlite3.Row]:
    """Cota armazenada mais recente do fundo. Retorna Row(data, cota) ou None."""
    cnpj_norm = "".join(ch for ch in str(cnpj) if ch.isdigit())
    with transacao(caminho) as con:
        return con.execute(
            "SELECT data, cota FROM cotas_fundos WHERE cnpj = ? "
            "ORDER BY data DESC LIMIT 1",
            (cnpj_norm,),
        ).fetchone()


def serie_cotas_fundo(cnpj: str, caminho: Optional[Path] = None) -> list[sqlite3.Row]:
    """Série completa (data, cota) armazenada do fundo, em ordem cronológica."""
    cnpj_norm = "".join(ch for ch in str(cnpj) if ch.isdigit())
    with transacao(caminho) as con:
        return con.execute(
            "SELECT data, cota FROM cotas_fundos WHERE cnpj = ? ORDER BY data",
            (cnpj_norm,),
        ).fetchall()


# --------------------------------------------------------------------------- #
# Cadastro de fundos da CVM (classe/tipo/taxa)
# --------------------------------------------------------------------------- #
def upsert_fundos_cadastro(registros: list[dict], caminho: Optional[Path] = None) -> int:
    """Upsert de registros de cadastro de fundos.

    Cada dict deve ter 'cnpj' e, opcionalmente, 'denominacao', 'classe', 'tipo',
    'taxa_adm', 'situacao', 'atualizado_em'. Retorna o número de linhas gravadas.
    """
    linhas = []
    for r in registros:
        cnpj_norm = "".join(ch for ch in str(r.get("cnpj", "")) if ch.isdigit())
        if not cnpj_norm:
            continue
        linhas.append(
            (
                cnpj_norm,
                r.get("denominacao"),
                r.get("classe"),
                r.get("tipo"),
                r.get("taxa_adm"),
                r.get("situacao"),
                r.get("atualizado_em") or date.today().isoformat(),
            )
        )
    if not linhas:
        return 0
    with transacao(caminho) as con:
        con.executemany(
            """
            INSERT INTO fundos_cadastro
                (cnpj, denominacao, classe, tipo, taxa_adm, situacao, atualizado_em)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cnpj) DO UPDATE SET
                denominacao = excluded.denominacao,
                classe = excluded.classe,
                tipo = excluded.tipo,
                taxa_adm = excluded.taxa_adm,
                situacao = excluded.situacao,
                atualizado_em = excluded.atualizado_em
            """,
            linhas,
        )
    return len(linhas)


def obter_fundo_cadastro(cnpj: str, caminho: Optional[Path] = None) -> Optional[sqlite3.Row]:
    cnpj_norm = "".join(ch for ch in str(cnpj) if ch.isdigit())
    with transacao(caminho) as con:
        return con.execute(
            "SELECT * FROM fundos_cadastro WHERE cnpj = ?", (cnpj_norm,)
        ).fetchone()


def contar_fundos_cadastro(caminho: Optional[Path] = None) -> int:
    with transacao(caminho) as con:
        return int(con.execute("SELECT COUNT(*) AS c FROM fundos_cadastro").fetchone()["c"])


# --------------------------------------------------------------------------- #
# Universo de retornos e estatísticas por classe (motor de recomendação)
# --------------------------------------------------------------------------- #
def gravar_universo_retornos(
    registros: list[dict], caminho: Optional[Path] = None
) -> int:
    """Upsert de retornos do universo. Cada dict: cnpj, janela, data_ref, retorno."""
    linhas = []
    for r in registros:
        cnpj_norm = "".join(ch for ch in str(r.get("cnpj", "")) if ch.isdigit())
        if not cnpj_norm or r.get("retorno") is None:
            continue
        linhas.append(
            (cnpj_norm, int(r["janela"]), r["data_ref"], float(r["retorno"]))
        )
    if not linhas:
        return 0
    with transacao(caminho) as con:
        con.executemany(
            """
            INSERT INTO universo_retornos (cnpj, janela, data_ref, retorno)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(cnpj, janela, data_ref) DO UPDATE SET retorno = excluded.retorno
            """,
            linhas,
        )
    return len(linhas)


def gravar_pares_classe(registros: list[dict], caminho: Optional[Path] = None) -> int:
    """Upsert de estatísticas por classe. Cada dict: classe, janela, data_ref,
    mediana, q1, q3, n."""
    linhas = [
        (
            r["classe"],
            int(r["janela"]),
            r["data_ref"],
            float(r["mediana"]),
            r.get("q1"),
            r.get("q3"),
            int(r["n"]),
        )
        for r in registros
        if r.get("classe") and r.get("mediana") is not None
    ]
    if not linhas:
        return 0
    with transacao(caminho) as con:
        con.executemany(
            """
            INSERT INTO pares_classe (classe, janela, data_ref, mediana, q1, q3, n)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(classe, janela, data_ref) DO UPDATE SET
                mediana = excluded.mediana, q1 = excluded.q1,
                q3 = excluded.q3, n = excluded.n
            """,
            linhas,
        )
    return len(linhas)


def obter_pares_classe(
    classe: str, janela: int, data_ref: Optional[str] = None, caminho: Optional[Path] = None
) -> Optional[sqlite3.Row]:
    """Estatísticas de uma classe/janela (a data_ref mais recente, se não informada)."""
    with transacao(caminho) as con:
        if data_ref:
            return con.execute(
                "SELECT * FROM pares_classe WHERE classe = ? AND janela = ? AND data_ref = ?",
                (classe, janela, data_ref),
            ).fetchone()
        return con.execute(
            "SELECT * FROM pares_classe WHERE classe = ? AND janela = ? "
            "ORDER BY data_ref DESC LIMIT 1",
            (classe, janela),
        ).fetchone()


def melhores_da_classe(
    classe: str,
    janela: int,
    retorno_minimo: float,
    data_ref: Optional[str] = None,
    limite: int = 50,
    caminho: Optional[Path] = None,
) -> list[sqlite3.Row]:
    """Fundos de uma classe/janela com retorno >= `retorno_minimo`, com dados de
    cadastro (denominação, taxa). Usado para sugerir alternativas."""
    with transacao(caminho) as con:
        if data_ref is None:
            linha = con.execute(
                "SELECT MAX(data_ref) AS d FROM universo_retornos WHERE janela = ?",
                (janela,),
            ).fetchone()
            data_ref = linha["d"] if linha else None
        if data_ref is None:
            return []
        return con.execute(
            """
            SELECT u.cnpj, u.retorno, f.denominacao, f.taxa_adm, f.classe
            FROM universo_retornos u
            JOIN fundos_cadastro f ON f.cnpj = u.cnpj
            WHERE u.janela = ? AND u.data_ref = ? AND f.classe = ? AND u.retorno >= ?
            ORDER BY u.retorno DESC
            LIMIT ?
            """,
            (janela, data_ref, classe, retorno_minimo, limite),
        ).fetchall()


def retornos_por_classe(
    classe: str, data_ref: Optional[str] = None, caminho: Optional[Path] = None
) -> list[sqlite3.Row]:
    """Todos os retornos (por CNPJ e janela) de uma classe numa data de ref.

    Retorna linhas (cnpj, janela, retorno, denominacao, taxa_adm). Usado para
    achar fundos acima da mediana em >= 2 janelas (sugestão de alternativas).
    """
    with transacao(caminho) as con:
        if data_ref is None:
            linha = con.execute(
                "SELECT MAX(data_ref) AS d FROM universo_retornos"
            ).fetchone()
            data_ref = linha["d"] if linha else None
        if data_ref is None:
            return []
        return con.execute(
            """
            SELECT u.cnpj, u.janela, u.retorno, f.denominacao, f.taxa_adm
            FROM universo_retornos u
            JOIN fundos_cadastro f ON f.cnpj = u.cnpj
            WHERE f.classe = ? AND u.data_ref = ?
            ORDER BY u.cnpj, u.janela
            """,
            (classe, data_ref),
        ).fetchall()


def todas_classes(caminho: Optional[Path] = None) -> list[sqlite3.Row]:
    """(cnpj, classe) de todos os fundos com classe conhecida (para agregação)."""
    with transacao(caminho) as con:
        return con.execute(
            "SELECT cnpj, classe FROM fundos_cadastro "
            "WHERE classe IS NOT NULL AND classe <> ''"
        ).fetchall()


def data_ref_universo(caminho: Optional[Path] = None) -> Optional[str]:
    """Data de referência mais recente já computada no universo de retornos."""
    with transacao(caminho) as con:
        linha = con.execute("SELECT MAX(data_ref) AS d FROM universo_retornos").fetchone()
        return linha["d"] if linha and linha["d"] else None


if __name__ == "__main__":  # smoke manual
    inicializar()
    print(f"Banco inicializado em {CAMINHO_BANCO}")
    print("Titulares:", [t["nome"] for t in listar_titulares()])
