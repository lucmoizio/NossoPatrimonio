"""Indicadores oficiais do Banco Central via API SGS.

Fonte única e oficial (princípio inegociável §2.1): nenhum dado é inventado.
Se a API cair, usamos cache em disco; se nem cache houver, lançamos
`ErroDadosMercado` — nunca uma estimativa silenciosa.

Séries SGS utilizadas:
    12   — CDI diário (% a.d.)
    432  — Selic meta (% a.a.)
    433  — IPCA mensal (%)
    4389 — CDI anualizado (% a.a.)

Endpoint: https://api.bcb.gov.br/dados/serie/bcdata.sgs.{serie}/dados
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import requests

# Séries SGS.
SERIE_CDI_DIARIO = 12
SERIE_SELIC_META = 432
SERIE_IPCA_MENSAL = 433
SERIE_CDI_ANUAL = 4389

_URL_SGS = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{serie}/dados"
_DIR_CACHE = Path(__file__).resolve().parent.parent / ".cache_mercado"
_TTL_SEGUNDOS = 12 * 3600  # 12 horas
_TIMEOUT = 20
_FMT_BCB = "%d/%m/%Y"  # a API SGS entrega datas em dd/mm/aaaa


class ErroDadosMercado(RuntimeError):
    """Falha ao obter um indicador oficial e sem cache disponível."""


@dataclass(frozen=True)
class IndicadoresAtuais:
    """Fotografia dos indicadores oficiais com carimbo de consulta."""

    selic_meta_aa: Optional[float]
    cdi_aa: Optional[float]
    ipca_12m: Optional[float]
    consultado_em: str
    fonte: str = "Banco Central do Brasil — API SGS"


# --------------------------------------------------------------------------- #
# Cache em disco
# --------------------------------------------------------------------------- #
def _chave_cache(serie: int, inicio: Optional[str], fim: Optional[str]) -> Path:
    nome = f"sgs_{serie}_{inicio or 'ini'}_{fim or 'fim'}.json"
    return _DIR_CACHE / nome


def _ler_cache(caminho: Path) -> Optional[dict]:
    if not caminho.exists():
        return None
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _gravar_cache(caminho: Path, dados: list[dict]) -> None:
    _DIR_CACHE.mkdir(parents=True, exist_ok=True)
    envelope = {"gravado_em": time.time(), "dados": dados}
    try:
        caminho.write_text(json.dumps(envelope), encoding="utf-8")
    except OSError:
        pass  # cache é best-effort; a ausência não é fatal


# --------------------------------------------------------------------------- #
# Acesso à API SGS
# --------------------------------------------------------------------------- #
def _buscar_serie(
    serie: int, inicio: Optional[str] = None, fim: Optional[str] = None
) -> list[dict]:
    """Busca uma série do SGS com cache (TTL 12h) e fallback para cache antigo.

    `inicio`/`fim` em ISO `yyyy-mm-dd`. Retorna lista de {"data": dd/mm/aaaa,
    "valor": "x,y"} conforme o SGS.
    """
    caminho = _chave_cache(serie, inicio, fim)
    cache = _ler_cache(caminho)
    if cache and (time.time() - cache.get("gravado_em", 0)) < _TTL_SEGUNDOS:
        return cache["dados"]

    url = _URL_SGS.format(serie=serie)
    params: dict[str, str] = {"formato": "json"}
    if inicio:
        params["dataInicial"] = datetime.fromisoformat(inicio).strftime(_FMT_BCB)
    if fim:
        params["dataFinal"] = datetime.fromisoformat(fim).strftime(_FMT_BCB)

    try:
        resp = requests.get(url, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        dados = resp.json()
        _gravar_cache(caminho, dados)
        return dados
    except (requests.RequestException, ValueError) as exc:
        # Fallback: cache antigo (mesmo expirado) mantém o sistema útil offline.
        if cache and cache.get("dados"):
            return cache["dados"]
        raise ErroDadosMercado(
            f"Não foi possível obter a série SGS {serie} do Banco Central "
            f"e não há cache disponível. Detalhe: {exc}"
        ) from exc


def _para_float(texto: str) -> float:
    """Converte '13,65' → 13.65 (formato numérico do SGS)."""
    return float(str(texto).replace(".", "").replace(",", ".")) if "," in str(texto) else float(texto)


# --------------------------------------------------------------------------- #
# Capitalização composta
# --------------------------------------------------------------------------- #
def _acumular(dados: list[dict]) -> float:
    """Capitaliza uma série de percentuais periódicos e retorna fração decimal.

    Ex.: pontos de CDI diário (% a.d.) → (1+r1)(1+r2)... − 1.
    """
    fator = 1.0
    for ponto in dados:
        taxa = _para_float(ponto["valor"]) / 100.0
        fator *= 1.0 + taxa
    return fator - 1.0


def cdi_acumulado(inicio: str, fim: str) -> float:
    """CDI acumulado (fração decimal) no intervalo [inicio, fim] em ISO.

    Usa a série diária 12 capitalizada de forma composta.
    """
    dados = _buscar_serie(SERIE_CDI_DIARIO, inicio, fim)
    return _acumular(dados)


def ipca_acumulado(inicio: str, fim: str) -> float:
    """IPCA acumulado (fração decimal) no intervalo [inicio, fim] em ISO.

    Usa a série mensal 433 capitalizada de forma composta.
    """
    dados = _buscar_serie(SERIE_IPCA_MENSAL, inicio, fim)
    return _acumular(dados)


def _ultimo_valor(serie: int) -> Optional[float]:
    """Retorna o último valor disponível de uma série (ou None se indisponível)."""
    try:
        dados = _buscar_serie(serie)
        if not dados:
            return None
        return _para_float(dados[-1]["valor"])
    except ErroDadosMercado:
        return None


def indicadores_atuais() -> IndicadoresAtuais:
    """Fotografia atual de Selic meta, CDI a.a. e IPCA 12m, com carimbo.

    Cada indicador é obtido de forma independente; um que falhe vem como None
    (a UI mostra 'indisponível'), sem contaminar os demais.
    """
    selic = _ultimo_valor(SERIE_SELIC_META)
    cdi = _ultimo_valor(SERIE_CDI_ANUAL)

    # IPCA 12 meses: acumula os últimos 12 pontos mensais da série 433.
    ipca_12m: Optional[float] = None
    try:
        dados = _buscar_serie(SERIE_IPCA_MENSAL)
        if dados:
            ultimos = dados[-12:]
            ipca_12m = round(_acumular(ultimos) * 100.0, 2)
    except ErroDadosMercado:
        ipca_12m = None

    return IndicadoresAtuais(
        selic_meta_aa=selic,
        cdi_aa=cdi,
        ipca_12m=ipca_12m,
        consultado_em=datetime.now().strftime("%d/%m/%Y %H:%M"),
    )


if __name__ == "__main__":  # smoke manual (requer rede)
    ind = indicadores_atuais()
    print(ind)
