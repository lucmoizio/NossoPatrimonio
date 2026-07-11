"""Paper trading da B3 (simulador com dinheiro fictício).

Cotações reais via yfinance (tickers B3 = código + '.SA', último fechamento).
Filosofia core-satellite: a carteira real é o núcleo conservador; o simulador
é o "satélite" onde se testam ideias sem risco. O teste da verdade é o
`vs_cdi`: bater o CDI acumulado desde o início, líquido de custos.

Travas de segurança: custo por ordem (default 0,03%), validação de caixa na
compra e de posição na venda, e alerta de limite de perda (default 20% do
saldo inicial).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from . import database, mercado


class ErroSimulador(RuntimeError):
    """Operação inválida no simulador (caixa/posição insuficiente etc.)."""


def ticker_b3(codigo: str) -> str:
    """Normaliza um código B3 para o formato do Yahoo Finance ('PETR4' → 'PETR4.SA')."""
    codigo = codigo.strip().upper()
    return codigo if codigo.endswith(".SA") else f"{codigo}.SA"


def preco_atual(codigo: str) -> Optional[float]:
    """Último fechamento do ticker via yfinance. None se indisponível."""
    try:
        import yfinance as yf
    except ImportError:  # dependência ausente: degrada com clareza
        return None

    simbolo = ticker_b3(codigo)
    try:
        hist = yf.Ticker(simbolo).history(period="5d")
        if hist is None or hist.empty:
            return None
        return float(hist["Close"].dropna().iloc[-1])
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Configuração do jogo
# --------------------------------------------------------------------------- #
def configurar(
    saldo_inicial: float,
    data_inicio: Optional[str] = None,
    custo_pct: float = 0.03,
    limite_perda_pct: float = 20.0,
) -> None:
    """Inicia/reconfigura o simulador (saldo fictício, data-base, custos, trava)."""
    database.salvar_sim_config(
        saldo_inicial=saldo_inicial,
        data_inicio=data_inicio or date.today().isoformat(),
        custo_pct=custo_pct,
        limite_perda_pct=limite_perda_pct,
    )


def reiniciar() -> None:
    """Zera ordens e configuração (recomeçar do zero)."""
    database.limpar_simulador()


# --------------------------------------------------------------------------- #
# Estado derivado das ordens
# --------------------------------------------------------------------------- #
@dataclass
class Posicao:
    ticker: str
    quantidade: float
    preco_medio: float


@dataclass
class EstadoSimulador:
    saldo_inicial: float
    caixa: float
    posicoes: dict[str, Posicao]


def _estado() -> Optional[EstadoSimulador]:
    """Reconstrói caixa e posições (preço médio) a partir das ordens."""
    cfg = database.obter_sim_config()
    if cfg is None:
        return None

    caixa = float(cfg["saldo_inicial"])
    posicoes: dict[str, Posicao] = {}
    for ordem in database.listar_sim_ordens():
        ticker = ordem["ticker"]
        qtd = float(ordem["quantidade"])
        preco = float(ordem["preco"])
        custos = float(ordem["custos"])
        pos = posicoes.get(ticker)
        if ordem["tipo"] == "compra":
            caixa -= qtd * preco + custos
            if pos is None:
                posicoes[ticker] = Posicao(ticker, qtd, preco)
            else:
                total = pos.quantidade + qtd
                pos.preco_medio = (
                    (pos.preco_medio * pos.quantidade + preco * qtd) / total
                    if total > 0 else 0.0
                )
                pos.quantidade = total
        else:  # venda
            caixa += qtd * preco - custos
            if pos is not None:
                pos.quantidade -= qtd
                if pos.quantidade <= 1e-9:
                    del posicoes[ticker]
    return EstadoSimulador(
        saldo_inicial=float(cfg["saldo_inicial"]), caixa=caixa, posicoes=posicoes
    )


def _custo_ordem(valor_bruto: float) -> float:
    cfg = database.obter_sim_config()
    pct = float(cfg["custo_pct"]) if cfg else 0.03
    return round(valor_bruto * pct / 100.0, 2)


# --------------------------------------------------------------------------- #
# Operações
# --------------------------------------------------------------------------- #
def comprar(codigo: str, quantidade: float, preco: Optional[float] = None) -> dict:
    """Registra uma compra. Valida caixa suficiente. Preço = mercado se None."""
    estado = _estado()
    if estado is None:
        raise ErroSimulador("Simulador não configurado. Defina o saldo inicial.")
    if quantidade <= 0:
        raise ErroSimulador("Quantidade deve ser positiva.")

    preco = preco if preco is not None else preco_atual(codigo)
    if preco is None:
        raise ErroSimulador(
            f"Preço de {ticker_b3(codigo)} indisponível (Yahoo/B3). Tente mais tarde."
        )

    bruto = quantidade * preco
    custos = _custo_ordem(bruto)
    if bruto + custos > estado.caixa + 1e-9:
        raise ErroSimulador(
            f"Caixa insuficiente: precisa de R$ {bruto + custos:,.2f}, "
            f"tem R$ {estado.caixa:,.2f}."
        )

    database.registrar_sim_ordem(
        date.today().isoformat(), ticker_b3(codigo), "compra", quantidade, preco, custos
    )
    return {"ticker": ticker_b3(codigo), "quantidade": quantidade, "preco": preco, "custos": custos}


def vender(codigo: str, quantidade: float, preco: Optional[float] = None) -> dict:
    """Registra uma venda. Valida posição suficiente. Preço = mercado se None."""
    estado = _estado()
    if estado is None:
        raise ErroSimulador("Simulador não configurado. Defina o saldo inicial.")
    if quantidade <= 0:
        raise ErroSimulador("Quantidade deve ser positiva.")

    ticker = ticker_b3(codigo)
    pos = estado.posicoes.get(ticker)
    if pos is None or pos.quantidade + 1e-9 < quantidade:
        tem = pos.quantidade if pos else 0.0
        raise ErroSimulador(f"Posição insuficiente em {ticker}: tem {tem}, quer vender {quantidade}.")

    preco = preco if preco is not None else preco_atual(codigo)
    if preco is None:
        raise ErroSimulador(f"Preço de {ticker} indisponível (Yahoo/B3). Tente mais tarde.")

    bruto = quantidade * preco
    custos = _custo_ordem(bruto)
    database.registrar_sim_ordem(
        date.today().isoformat(), ticker, "venda", quantidade, preco, custos
    )
    return {"ticker": ticker, "quantidade": quantidade, "preco": preco, "custos": custos}


# --------------------------------------------------------------------------- #
# Placar
# --------------------------------------------------------------------------- #
@dataclass
class ResultadoSimulador:
    saldo_inicial: float
    caixa: float
    valor_posicoes: float
    patrimonio: float
    rent_pct: float
    cdi_periodo_pct: Optional[float]     # CDI acumulado desde o início
    vs_cdi_pp: Optional[float]           # diferença em pontos percentuais
    trava_atingida: bool
    limite_perda_pct: float
    posicoes: list[dict] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)


def resultado() -> Optional[ResultadoSimulador]:
    """Marca a mercado e compara com o CDI acumulado desde o início.

    Retorna None se o simulador não estiver configurado.
    """
    cfg = database.obter_sim_config()
    estado = _estado()
    if cfg is None or estado is None:
        return None

    avisos: list[str] = []
    valor_posicoes = 0.0
    posicoes_detalhe: list[dict] = []
    for ticker, pos in estado.posicoes.items():
        preco = preco_atual(ticker)
        if preco is None:
            avisos.append(f"Preço de {ticker} indisponível — posição marcada pelo preço médio.")
            preco = pos.preco_medio
        valor = pos.quantidade * preco
        valor_posicoes += valor
        posicoes_detalhe.append(
            {
                "ticker": ticker,
                "quantidade": pos.quantidade,
                "preco_medio": round(pos.preco_medio, 2),
                "preco_atual": round(preco, 2),
                "valor": round(valor, 2),
                "resultado": round((preco - pos.preco_medio) * pos.quantidade, 2),
            }
        )

    patrimonio = estado.caixa + valor_posicoes
    saldo_inicial = float(cfg["saldo_inicial"])
    rent = (patrimonio / saldo_inicial - 1.0) if saldo_inicial > 0 else 0.0

    # Benchmark: CDI acumulado desde o início do jogo.
    cdi_pct: Optional[float] = None
    vs_cdi: Optional[float] = None
    try:
        cdi = mercado.cdi_acumulado(cfg["data_inicio"], date.today().isoformat())
        cdi_pct = round(cdi * 100, 2)
        vs_cdi = round((rent - cdi) * 100, 2)
    except mercado.ErroDadosMercado:
        avisos.append("CDI do período indisponível — comparação com o benchmark suspensa.")

    limite = float(cfg["limite_perda_pct"])
    trava = rent * 100 <= -limite
    if trava:
        avisos.append(
            f"TRAVA DE PERDA atingida: queda de {abs(rent) * 100:.1f}% "
            f"(limite {limite:.0f}%). Revise a estratégia."
        )

    return ResultadoSimulador(
        saldo_inicial=round(saldo_inicial, 2),
        caixa=round(estado.caixa, 2),
        valor_posicoes=round(valor_posicoes, 2),
        patrimonio=round(patrimonio, 2),
        rent_pct=round(rent * 100, 2),
        cdi_periodo_pct=cdi_pct,
        vs_cdi_pp=vs_cdi,
        trava_atingida=trava,
        limite_perda_pct=limite,
        posicoes=posicoes_detalhe,
        avisos=avisos,
    )
