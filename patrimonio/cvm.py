"""Atualização automática de fundos via Informe Diário da CVM.

Fonte oficial (§2.1): Informe Diário de Fundos de Investimento Financeiro (FIF)
    https://dados.cvm.gov.br/dados/FI/DOC/INF_DIARIO/DADOS/inf_diario_fi_{AAAAMM}.zip

Formato: ZIP com CSV separado por ';', encoding latin-1. Colunas relevantes:
    CNPJ_FUNDO_CLASSE (layout pós-CVM 175; fallback CNPJ_FUNDO em arquivos antigos),
    DT_COMPTC (data da competência, yyyy-mm-dd), VL_QUOTA (valor da cota).

Regra de atualização de valor:
    valor_novo = valor_ultimo_snapshot × (cota_atual / cota_na_data_do_snapshot)

A cota de referência é a do pregão <= data do snapshot. Premissa: nenhuma
movimentação desde a referência (se houver, o usuário grava um snapshot manual
que passa a ser a nova referência).

Cache mensal em disco: mês corrente com TTL 12h; meses fechados são imutáveis.

Limitação conhecida: FIDCs não constam do Informe Diário de FIF (divulgação
própria mensal) — hoje ficam manuais (roadmap R6).
"""

from __future__ import annotations

import csv
import io
import time
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import requests

_URL_INFORME = (
    "https://dados.cvm.gov.br/dados/FI/DOC/INF_DIARIO/DADOS/inf_diario_fi_{aaaamm}.zip"
)
_DIR_CACHE = Path(__file__).resolve().parent.parent / ".cache_cvm"
_TTL_MES_CORRENTE = 12 * 3600  # 12h
_TIMEOUT = 60

_COLS_CNPJ = ("CNPJ_FUNDO_CLASSE", "CNPJ_FUNDO")
_COL_DATA = "DT_COMPTC"
_COL_COTA = "VL_QUOTA"


class ErroDadosCVM(RuntimeError):
    """Falha ao obter ou interpretar o Informe Diário da CVM."""


def _so_digitos(cnpj: str) -> str:
    """Normaliza um CNPJ para apenas dígitos (remove pontuação)."""
    return "".join(ch for ch in str(cnpj) if ch.isdigit())


def _caminho_cache(aaaamm: str) -> Path:
    return _DIR_CACHE / f"inf_diario_fi_{aaaamm}.zip"


def _mes_corrente(aaaamm: str) -> bool:
    return aaaamm == date.today().strftime("%Y%m")


def _baixar_informe(aaaamm: str) -> bytes:
    """Baixa (com cache mensal) o ZIP do informe do mês `aaaamm` (AAAAMM)."""
    caminho = _caminho_cache(aaaamm)

    if caminho.exists():
        idade = time.time() - caminho.stat().st_mtime
        # Meses fechados são imutáveis; mês corrente respeita TTL de 12h.
        if not _mes_corrente(aaaamm) or idade < _TTL_MES_CORRENTE:
            return caminho.read_bytes()

    url = _URL_INFORME.format(aaaamm=aaaamm)
    try:
        resp = requests.get(url, timeout=_TIMEOUT)
        resp.raise_for_status()
        conteudo = resp.content
        _DIR_CACHE.mkdir(parents=True, exist_ok=True)
        caminho.write_bytes(conteudo)
        return conteudo
    except requests.RequestException as exc:
        if caminho.exists():  # fallback: cache mesmo expirado
            return caminho.read_bytes()
        raise ErroDadosCVM(
            f"Não foi possível baixar o Informe Diário CVM de {aaaamm}. "
            f"Detalhe: {exc}"
        ) from exc


def _ler_cotas_do_mes(aaaamm: str, cnpj_alvo: str) -> dict[str, float]:
    """Retorna {data_iso: cota} do fundo `cnpj_alvo` no mês `aaaamm`."""
    conteudo = _baixar_informe(aaaamm)
    alvo = _so_digitos(cnpj_alvo)
    cotas: dict[str, float] = {}

    try:
        with zipfile.ZipFile(io.BytesIO(conteudo)) as zf:
            nomes_csv = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not nomes_csv:
                raise ErroDadosCVM(f"ZIP da CVM de {aaaamm} sem CSV.")
            for nome in nomes_csv:
                with zf.open(nome) as bruto:
                    texto = io.TextIOWrapper(bruto, encoding="latin-1", newline="")
                    leitor = csv.DictReader(texto, delimiter=";")
                    col_cnpj = next(
                        (c for c in _COLS_CNPJ if c in (leitor.fieldnames or [])),
                        None,
                    )
                    if col_cnpj is None:
                        continue
                    for linha in leitor:
                        if _so_digitos(linha.get(col_cnpj, "")) != alvo:
                            continue
                        data_str = (linha.get(_COL_DATA) or "").strip()
                        cota_str = (linha.get(_COL_COTA) or "").strip()
                        if not data_str or not cota_str:
                            continue
                        try:
                            cotas[data_str] = float(cota_str.replace(",", "."))
                        except ValueError:
                            continue
    except zipfile.BadZipFile as exc:
        raise ErroDadosCVM(
            f"Arquivo da CVM de {aaaamm} corrompido/inválido: {exc}"
        ) from exc
    return cotas


def _meses_entre(inicio: date, fim: date) -> list[str]:
    """Lista de 'AAAAMM' de `inicio` a `fim` inclusive."""
    meses: list[str] = []
    ano, mes = inicio.year, inicio.month
    while (ano, mes) <= (fim.year, fim.month):
        meses.append(f"{ano:04d}{mes:02d}")
        mes += 1
        if mes > 12:
            mes = 1
            ano += 1
    return meses


def cota_na_data(cnpj: str, data_ref: str) -> Optional[tuple[str, float]]:
    """Cota do fundo no pregão <= `data_ref` (ISO). Retorna (data, cota) ou None.

    Procura no mês da data e retrocede até 3 meses para achar o último pregão
    disponível igual ou anterior à data solicitada.
    """
    alvo = datetime.fromisoformat(data_ref).date()
    for recuo in range(0, 4):
        ano = alvo.year
        mes = alvo.month - recuo
        while mes <= 0:
            mes += 12
            ano -= 1
        aaaamm = f"{ano:04d}{mes:02d}"
        try:
            cotas = _ler_cotas_do_mes(aaaamm, cnpj)
        except ErroDadosCVM:
            continue
        candidatos = {
            d: c for d, c in cotas.items()
            if datetime.fromisoformat(d).date() <= alvo
        }
        if candidatos:
            data_mais_recente = max(candidatos)
            return data_mais_recente, candidatos[data_mais_recente]
    return None


def cota_mais_recente(cnpj: str) -> Optional[tuple[str, float]]:
    """Cota mais recente disponível do fundo (busca mês corrente e anteriores)."""
    hoje = date.today()
    for recuo in range(0, 4):
        ano = hoje.year
        mes = hoje.month - recuo
        while mes <= 0:
            mes += 12
            ano -= 1
        aaaamm = f"{ano:04d}{mes:02d}"
        try:
            cotas = _ler_cotas_do_mes(aaaamm, cnpj)
        except ErroDadosCVM:
            continue
        if cotas:
            data_mais_recente = max(cotas)
            return data_mais_recente, cotas[data_mais_recente]
    return None


def novo_valor_estimado(
    cnpj: str, data_snapshot: str, valor_snapshot: float
) -> Optional[dict]:
    """Estima o valor atual do fundo a partir do último snapshot.

    valor_novo = valor_snapshot × (cota_atual / cota_na_data_do_snapshot)

    Retorna dict com detalhes (para exibição rotulada na UI) ou None se as
    cotas necessárias não estiverem disponíveis na CVM (ex.: FIDC).
    """
    ref = cota_na_data(cnpj, data_snapshot)
    atual = cota_mais_recente(cnpj)
    if ref is None or atual is None:
        return None
    data_ref_cota, cota_ref = ref
    data_atual_cota, cota_atual = atual
    if cota_ref <= 0:
        return None
    valor_novo = valor_snapshot * (cota_atual / cota_ref)
    return {
        "valor_novo": round(valor_novo, 2),
        "cota_ref": cota_ref,
        "data_cota_ref": data_ref_cota,
        "cota_atual": cota_atual,
        "data_cota_atual": data_atual_cota,
        "variacao": cota_atual / cota_ref - 1.0,
    }


if __name__ == "__main__":  # smoke manual (requer rede)
    # CNPJ de exemplo; substitua por um fundo real para testar.
    print(cota_mais_recente("00.000.000/0001-00"))
