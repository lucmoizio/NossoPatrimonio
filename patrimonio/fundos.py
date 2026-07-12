"""Camada normalizada de cotas de fundos (fonte única e oficial: CVM).

Este módulo desacopla o resto do sistema da origem dos dados: define um formato
único (`Cotacao`) e um contrato (`FonteCotas`). Hoje há uma única implementação,
`FonteCVM`, sobre o Informe Diário (`cvm.py`) — coerente com o princípio de usar
apenas fontes oficiais. Novas fontes poderiam ser adicionadas no futuro sem
mudar o motor de atualização, desde que respeitem o mesmo contrato.

`sincronizar_cotas` é idempotente: só busca o que falta desde a última cota
gravada em `cotas_fundos` e faz upsert (chave cnpj+data).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional, Protocol

from . import cvm, database


@dataclass(frozen=True)
class Cotacao:
    """Uma observação diária de um fundo, independente da origem."""

    cnpj: str
    data: str  # ISO yyyy-mm-dd
    cota: float
    pl: Optional[float] = None
    cotistas: Optional[int] = None
    fonte: str = "CVM"


class FonteCotas(Protocol):
    """Contrato de uma fonte de cotas de fundos."""

    nome: str

    def cotas_desde(self, cnpj: str, data_min: str, data_max: Optional[str] = None) -> list[Cotacao]:
        """Cotas do fundo com data no intervalo (data_min, data_max]."""
        ...


class FonteCVM:
    """Fonte oficial: Informe Diário de Fundos (FIF) da CVM."""

    nome = "CVM"

    def cotas_desde(
        self, cnpj: str, data_min: str, data_max: Optional[str] = None
    ) -> list[Cotacao]:
        pontos = cvm.serie_cotas_fundo(cnpj, data_min, data_max)
        return [
            Cotacao(
                cnpj=cnpj,
                data=p["data"],
                cota=p["cota"],
                pl=p.get("pl"),
                cotistas=p.get("cotistas"),
                fonte=self.nome,
            )
            for p in pontos
        ]


@dataclass
class ResultadoSync:
    """Resumo da sincronização de um fundo."""

    cnpj: str
    novas: int
    ultima_data: Optional[str]
    erro: Optional[str] = None


def sincronizar_cotas(
    cnpjs: list[str],
    fonte: Optional[FonteCotas] = None,
    dias_historico_inicial: int = 400,
    caminho=None,
) -> list[ResultadoSync]:
    """Sincroniza as cotas dos `cnpjs` no cache local (`cotas_fundos`).

    Para cada fundo, busca desde o dia seguinte à última cota gravada (ou desde
    `dias_historico_inicial` dias atrás, no primeiro sync) até hoje, e grava o
    que faltar. Idempotente: reexecutar não duplica (upsert por cnpj+data).
    """
    fonte = fonte or FonteCVM()
    hoje = date.today().isoformat()
    resultados: list[ResultadoSync] = []

    for cnpj in cnpjs:
        cnpj_norm = "".join(ch for ch in str(cnpj) if ch.isdigit())
        if not cnpj_norm:
            continue
        ultima = database.ultima_data_cota(cnpj_norm, caminho=caminho)
        if ultima:
            data_min = (
                datetime.fromisoformat(ultima).date() + timedelta(days=1)
            ).isoformat()
        else:
            data_min = (date.today() - timedelta(days=dias_historico_inicial)).isoformat()

        if data_min > hoje:
            resultados.append(ResultadoSync(cnpj_norm, 0, ultima))
            continue

        try:
            cotacoes = fonte.cotas_desde(cnpj_norm, data_min, hoje)
        except cvm.ErroDadosCVM as exc:
            resultados.append(ResultadoSync(cnpj_norm, 0, ultima, erro=str(exc)))
            continue

        if cotacoes:
            database.gravar_cotas_fundo(
                cnpj_norm,
                [
                    {"data": c.data, "cota": c.cota, "pl": c.pl, "cotistas": c.cotistas}
                    for c in cotacoes
                ],
                fonte=fonte.nome,
                caminho=caminho,
            )
        nova_ultima = database.ultima_data_cota(cnpj_norm, caminho=caminho) or ultima
        resultados.append(ResultadoSync(cnpj_norm, len(cotacoes), nova_ultima))

    return resultados


if __name__ == "__main__":  # smoke manual (requer rede)
    import sys

    database.inicializar()
    alvo = sys.argv[1:] or []
    if alvo:
        for r in sincronizar_cotas(alvo):
            print(r)
