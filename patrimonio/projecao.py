"""Projeções de patrimônio por juros compostos e cenários.

Todos os resultados aqui são **projeções** (rotuladas como tal na UI). Os
cenários padrão derivam do CDI oficial atual (80% / 95% / 110% do CDI
líquido), nunca de um número inventado.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from . import mercado

# IR de longo prazo (fundos > 720 dias / renda fixa longa) para "CDI líquido".
ALIQUOTA_IR_LONGO = 0.15


@dataclass
class PontoProjecao:
    """Valor projetado ao fim de um ano."""

    ano: int
    valor: float


@dataclass
class Cenario:
    """Cenário nomeado de projeção (rótulo, taxa a.a. e trajetória)."""

    nome: str
    taxa_aa: float
    trajetoria: list[PontoProjecao]
    valor_final: float
    anos_ate_meta: Optional[float]


def evolucao(
    valor_inicial: float, aporte_anual: float, taxa_aa: float, anos: int
) -> list[PontoProjecao]:
    """Evolução patrimonial ano a ano (aporte creditado ao fim de cada ano).

    valor_{n} = valor_{n-1} × (1 + taxa) + aporte_anual
    """
    trajetoria: list[PontoProjecao] = []
    valor = valor_inicial
    for ano in range(1, anos + 1):
        valor = valor * (1.0 + taxa_aa) + aporte_anual
        trajetoria.append(PontoProjecao(ano=ano, valor=round(valor, 2)))
    return trajetoria


def anos_ate_meta(
    valor_inicial: float,
    aporte_anual: float,
    taxa_aa: float,
    valor_alvo: float,
    limite_anos: int = 100,
) -> Optional[float]:
    """Anos até atingir `valor_alvo`, com interpolação linear intra-ano.

    Retorna None se a meta não for atingível dentro de `limite_anos`.
    """
    if valor_inicial >= valor_alvo:
        return 0.0
    valor = valor_inicial
    for ano in range(1, limite_anos + 1):
        anterior = valor
        valor = valor * (1.0 + taxa_aa) + aporte_anual
        if valor >= valor_alvo:
            # Interpola fração do ano em que a meta foi cruzada.
            if valor == anterior:
                return float(ano)
            fracao = (valor_alvo - anterior) / (valor - anterior)
            return round(ano - 1 + fracao, 2)
    return None


def tempo_para_dobrar(taxa_aa: float) -> Optional[float]:
    """Tempo exato (anos) para dobrar o capital a uma taxa composta.

    t = ln(2) / ln(1 + i). Retorna None para taxa <= 0.
    """
    if taxa_aa <= 0:
        return None
    return round(math.log(2) / math.log(1.0 + taxa_aa), 2)


def cdi_liquido_atual() -> Optional[float]:
    """CDI líquido de IR de longo prazo (fração a.a.), a partir do CDI oficial.

    Retorna None se o CDI oficial não estiver disponível.
    """
    ind = mercado.indicadores_atuais()
    if ind.cdi_aa is None:
        return None
    cdi_bruto = ind.cdi_aa / 100.0
    return cdi_bruto * (1.0 - ALIQUOTA_IR_LONGO)


def cenarios_padrao(
    valor_inicial: float,
    aporte_anual: float,
    anos: int,
    valor_alvo: float,
    cdi_liquido: Optional[float] = None,
) -> list[Cenario]:
    """Três cenários derivados do CDI líquido: 80% / 95% / 110%.

    Se `cdi_liquido` não for informado nem obtido, retorna lista vazia (a UI
    deve declarar indisponibilidade — não inventamos taxas).
    """
    if cdi_liquido is None:
        cdi_liquido = cdi_liquido_atual()
    if cdi_liquido is None:
        return []

    definicoes = (
        ("Conservador (80% do CDI líq.)", 0.80),
        ("Base (95% do CDI líq.)", 0.95),
        ("Otimista (110% do CDI líq.)", 1.10),
    )
    cenarios: list[Cenario] = []
    for nome, fator in definicoes:
        taxa = cdi_liquido * fator
        traj = evolucao(valor_inicial, aporte_anual, taxa, anos)
        cenarios.append(
            Cenario(
                nome=nome,
                taxa_aa=taxa,
                trajetoria=traj,
                valor_final=traj[-1].valor if traj else valor_inicial,
                anos_ate_meta=anos_ate_meta(valor_inicial, aporte_anual, taxa, valor_alvo),
            )
        )
    return cenarios
