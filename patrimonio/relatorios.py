"""Relatórios: evolução patrimonial, proventos (renda passiva) e IR anual.

O relatório de IR é um **auxílio de preenchimento** — os Informes de
Rendimentos oficiais das instituições sempre prevalecem (aviso explícito).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import pandas as pd

from . import database


# --------------------------------------------------------------------------- #
# Evolução patrimonial
# --------------------------------------------------------------------------- #
def evolucao_patrimonial(titular_id: Optional[int] = None) -> pd.DataFrame:
    """Série temporal do patrimônio total por data.

    Faz pivot dos snapshots (linhas=data, colunas=ativo), aplica forward-fill
    (o último valor conhecido de cada ativo vale até o próximo snapshot) e
    soma por linha, produzindo o total da carteira em cada data observada.

    Retorna DataFrame com colunas ['data', 'total'] (vazio se não houver dados).
    """
    linhas = database.todos_snapshots()
    if titular_id is not None:
        linhas = [r for r in linhas if int(r["titular_id"]) == titular_id]
    if not linhas:
        return pd.DataFrame(columns=["data", "total"])

    df = pd.DataFrame(
        [{"data": r["data"], "ativo_id": r["ativo_id"], "valor": r["valor"]} for r in linhas]
    )
    df["data"] = pd.to_datetime(df["data"])
    pivot = df.pivot_table(index="data", columns="ativo_id", values="valor", aggfunc="last")
    pivot = pivot.sort_index().ffill()
    total = pivot.sum(axis=1).reset_index()
    total.columns = ["data", "total"]
    return total


# --------------------------------------------------------------------------- #
# Proventos (renda passiva)
# --------------------------------------------------------------------------- #
@dataclass
class ResumoProventos:
    """Agregações de proventos para o painel de renda passiva."""

    total_ano: float
    media_mensal: float
    por_tipo: dict[str, float]
    por_mes: dict[str, float]


def resumo_proventos(ano: int, titular_id: Optional[int] = None) -> ResumoProventos:
    """Total do ano, média mensal e quebras por tipo e por mês."""
    registros = database.listar_proventos(ano=ano)
    if titular_id is not None:
        registros = [r for r in registros if int(r["titular_id"]) == titular_id]

    total = sum(float(r["valor"]) for r in registros)
    por_tipo: dict[str, float] = {}
    por_mes: dict[str, float] = {}
    for r in registros:
        por_tipo[r["tipo"]] = por_tipo.get(r["tipo"], 0.0) + float(r["valor"])
        mes = str(r["data"])[:7]  # yyyy-mm
        por_mes[mes] = por_mes.get(mes, 0.0) + float(r["valor"])

    return ResumoProventos(
        total_ano=round(total, 2),
        media_mensal=round(total / 12.0, 2),
        por_tipo=por_tipo,
        por_mes=dict(sorted(por_mes.items())),
    )


# --------------------------------------------------------------------------- #
# Relatório de IR (auxílio de preenchimento)
# --------------------------------------------------------------------------- #
def _valor_em(ativo_id: int, ano: int) -> float:
    """Valor do ativo na posição de 31/12 do ano (último snapshot <= 31/12).

    Se não houver snapshot no ano ou antes, retorna 0.0.
    """
    limite = f"{ano}-12-31"
    snaps = database.listar_snapshots(ativo_id)
    candidatos = [s for s in snaps if s["data"] <= limite]
    return float(candidatos[-1]["valor"]) if candidatos else 0.0


@dataclass
class LinhaBemDireito:
    """Uma linha da ficha Bens e Direitos (situação em 31/12 de dois anos)."""

    ativo_id: int
    nome: str
    titular: str
    categoria: str
    cnpj: Optional[str]
    situacao_ano_anterior: float
    situacao_ano_base: float


@dataclass
class RelatorioIR:
    """Relatório anual de IR: posições e proventos, com aviso de prevalência."""

    ano_base: int
    ano_anterior: int
    bens_direitos: list[LinhaBemDireito]
    proventos_por_tipo: dict[str, float]
    total_proventos: float
    aviso: str = (
        "Estimativa de apoio ao preenchimento. Os Informes de Rendimentos "
        "oficiais das instituições prevalecem sobre estes valores."
    )
    campos_ficha: list[str] = field(
        default_factory=lambda: [
            "Situação em 31/12 (ano anterior)",
            "Situação em 31/12 (ano-base)",
        ]
    )


def relatorio_ir(ano_base: int, titular_id: Optional[int] = None) -> RelatorioIR:
    """Monta o relatório de IR do ano-base (posições 31/12 e proventos)."""
    ano_anterior = ano_base - 1
    ativos = database.listar_ativos(titular_id=titular_id, apenas_ativos=False)

    bens: list[LinhaBemDireito] = []
    for a in ativos:
        anterior = _valor_em(int(a["id"]), ano_anterior)
        base = _valor_em(int(a["id"]), ano_base)
        if anterior == 0.0 and base == 0.0:
            continue
        bens.append(
            LinhaBemDireito(
                ativo_id=int(a["id"]),
                nome=a["nome"],
                titular=a["titular_nome"],
                categoria=a["categoria"],
                cnpj=a["cnpj"],
                situacao_ano_anterior=round(anterior, 2),
                situacao_ano_base=round(base, 2),
            )
        )

    resumo = resumo_proventos(ano_base, titular_id=titular_id)
    return RelatorioIR(
        ano_base=ano_base,
        ano_anterior=ano_anterior,
        bens_direitos=bens,
        proventos_por_tipo=resumo.por_tipo,
        total_proventos=resumo.total_ano,
    )
