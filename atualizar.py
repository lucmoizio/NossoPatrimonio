#!/usr/bin/env python3
"""Motor de atualização automática (executável por cron/launchd).

Fontes exclusivamente oficiais (CVM + BCB), coerente com os princípios do
projeto. O que faz, em ordem:

1. (opcional) sincroniza o cadastro de fundos da CVM (classe/tipo) — `--cadastro`;
2. sincroniza as cotas dos fundos da carteira no cache local (`cotas_fundos`),
   com retry/backoff — idempotente (upsert por cnpj+data);
3. grava um novo snapshot de valor por ativo com CNPJ, a partir da variação de
   cota desde o último snapshot (mesma regra da aba "Atualizar valores");
4. detecta defasagem: fundos cujo dado mais novo passou de N dias úteis geram
   alerta (a CVM publica o Informe Diário com ~1 dia útil de atraso).

Idempotência: reexecutar no mesmo dia não duplica cotas nem snapshots
(`cotas_fundos` tem PK cnpj+data; `snapshots` tem UNIQUE(ativo_id, data)).

Uso:
    python atualizar.py                 # sincroniza cotas + snapshots
    python atualizar.py --cadastro      # também atualiza o cadastro de classes
    python atualizar.py --defasagem 7   # muda o limite de dias úteis do alerta
"""

from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional, TypeVar

from patrimonio import cadastro_cvm, cvm, database, fundos, recomendacao

_LOG_PATH = Path(__file__).resolve().parent / "atualizar.log"
_DEFASAGEM_PADRAO = 5  # dias úteis

logger = logging.getLogger("atualizar")

T = TypeVar("T")


def _configurar_log(verboso: bool) -> None:
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    if not any(isinstance(h, logging.FileHandler) for h in logger.handlers):
        fh = logging.FileHandler(_LOG_PATH, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    if verboso and not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in logger.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(sh)


def _com_retry(
    fn: Callable[[], T], tentativas: int = 3, espera_inicial: float = 5.0
) -> T:
    """Executa `fn` com retry e backoff exponencial (para falhas de rede)."""
    ultima_exc: Optional[Exception] = None
    espera = espera_inicial
    for i in range(1, tentativas + 1):
        try:
            return fn()
        except Exception as exc:  # rede/IO: tenta de novo
            ultima_exc = exc
            logger.warning("Tentativa %d/%d falhou: %s", i, tentativas, exc)
            if i < tentativas:
                time.sleep(espera)
                espera *= 2
    assert ultima_exc is not None
    raise ultima_exc


def _dias_uteis(d0: date, d1: date) -> int:
    """Dias úteis (seg-sex) entre d0 e d1 (aproximação: ignora feriados)."""
    if d1 <= d0:
        return 0
    dias = 0
    atual = d0
    while atual < d1:
        atual += timedelta(days=1)
        if atual.weekday() < 5:
            dias += 1
    return dias


def _cnpjs_carteira(caminho=None) -> list[str]:
    """CNPJs distintos de fundos ativos da carteira."""
    cnpjs: set[str] = set()
    for a in database.listar_ativos(apenas_ativos=True, caminho=caminho):
        c = "".join(ch for ch in str(a["cnpj"] or "") if ch.isdigit())
        if len(c) == 14:
            cnpjs.add(c)
    return sorted(cnpjs)


@dataclass
class ResultadoAtualizacao:
    """Resumo da execução do job de atualização."""

    fundos: int
    cotas_novas: int
    snapshots_gravados: int
    defasados: list[tuple[str, str, int]]  # (cnpj, ultima_data, dias_uteis)
    erros: list[tuple[str, str]]           # (cnpj, mensagem)


def atualizar(
    sync_cadastro: bool = False,
    construir_universo: bool = False,
    dias_defasagem: int = _DEFASAGEM_PADRAO,
    caminho=None,
) -> ResultadoAtualizacao:
    """Executa a atualização completa e retorna um resumo."""
    database.inicializar(caminho)

    if sync_cadastro:
        logger.info("Sincronizando cadastro de fundos da CVM...")
        try:
            n = _com_retry(lambda: cadastro_cvm.sincronizar_cadastro_fundos(caminho=caminho))
            logger.info("Cadastro: %d registros.", n)
        except Exception as exc:
            logger.error("Falha ao sincronizar cadastro: %s", exc)

    cnpjs = _cnpjs_carteira(caminho)
    logger.info("Carteira: %d fundos com CNPJ.", len(cnpjs))

    # 2) Sincroniza cotas com retry para os que falharem.
    resultados = fundos.sincronizar_cotas(cnpjs, caminho=caminho)
    erros = [(r.cnpj, r.erro) for r in resultados if r.erro]
    if erros:
        pendentes = [c for c, _ in erros]
        logger.info("Retry de %d fundos que falharam...", len(pendentes))
        try:
            retry = _com_retry(lambda: fundos.sincronizar_cotas(pendentes, caminho=caminho))
            resolvidos = {r.cnpj for r in retry if not r.erro}
            erros = [(c, m) for c, m in erros if c not in resolvidos]
            resultados = [r for r in resultados if r.cnpj not in resolvidos] + retry
        except Exception as exc:
            logger.error("Retry falhou: %s", exc)

    cotas_novas = sum(r.novas for r in resultados)

    # 3) Grava snapshot por ativo com base na variação de cota.
    snaps = 0
    for a in database.listar_ativos(apenas_ativos=True, caminho=caminho):
        cnpj = "".join(ch for ch in str(a["cnpj"] or "") if ch.isdigit())
        if len(cnpj) != 14:
            continue
        ultimo = database.ultimo_snapshot(int(a["id"]), caminho=caminho)
        if ultimo is None:
            continue
        try:
            est = cvm.novo_valor_estimado(cnpj, ultimo["data"], float(ultimo["valor"]))
        except cvm.ErroDadosCVM:
            est = None
        if est and est["data_cota_atual"] > ultimo["data"]:
            database.registrar_snapshot(
                int(a["id"]), est["data_cota_atual"], est["valor_novo"], caminho=caminho
            )
            snaps += 1

    # 3b) Universo de pares por classe (pesado; opcional).
    if construir_universo:
        logger.info("Construindo universo de retornos por classe (pode demorar)...")
        try:
            resumo_uni = _com_retry(lambda: recomendacao.estatisticas_classe(caminho=caminho))
            logger.info("Universo: %s", resumo_uni)
        except Exception as exc:
            logger.error("Falha ao construir universo: %s", exc)

    # 4) Detecção de defasagem.
    hoje = date.today()
    defasados: list[tuple[str, str, int]] = []
    for cnpj in cnpjs:
        ultima = database.ultima_data_cota(cnpj, caminho=caminho)
        if not ultima:
            defasados.append((cnpj, "—", 999))
            continue
        du = _dias_uteis(datetime.fromisoformat(ultima).date(), hoje)
        if du > dias_defasagem:
            defasados.append((cnpj, ultima, du))

    resumo = ResultadoAtualizacao(
        fundos=len(cnpjs),
        cotas_novas=cotas_novas,
        snapshots_gravados=snaps,
        defasados=defasados,
        erros=erros,
    )
    logger.info(
        "Concluído: %d fundos, %d cotas novas, %d snapshots, %d defasados, %d erros.",
        resumo.fundos, resumo.cotas_novas, resumo.snapshots_gravados,
        len(resumo.defasados), len(resumo.erros),
    )
    return resumo


def main() -> int:
    parser = argparse.ArgumentParser(description="Atualização automática (CVM+BCB).")
    parser.add_argument("--cadastro", action="store_true", help="também sincroniza o cadastro de fundos")
    parser.add_argument("--universo", action="store_true", help="reconstrói o universo de pares por classe (pesado)")
    parser.add_argument("--defasagem", type=int, default=_DEFASAGEM_PADRAO, help="limite de dias úteis para alerta de defasagem")
    parser.add_argument("--silencioso", action="store_true", help="não imprime no stdout (só no log)")
    args = parser.parse_args()

    _configurar_log(verboso=not args.silencioso)
    resumo = atualizar(
        sync_cadastro=args.cadastro,
        construir_universo=args.universo,
        dias_defasagem=args.defasagem,
    )

    print(
        f"Fundos: {resumo.fundos} | Cotas novas: {resumo.cotas_novas} | "
        f"Snapshots: {resumo.snapshots_gravados}"
    )
    if resumo.defasados:
        print(f"Defasados (> {args.defasagem} dias úteis):")
        for cnpj, ultima, du in resumo.defasados:
            print(f"  - {cadastro_cvm.formatar_cnpj(cnpj)}: última cota {ultima} ({du} dias úteis)")
    if resumo.erros:
        print("Erros:")
        for cnpj, msg in resumo.erros:
            print(f"  - {cadastro_cvm.formatar_cnpj(cnpj)}: {msg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
