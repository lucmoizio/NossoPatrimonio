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


def cotas_para_cnpjs(
    cnpjs: set[str], data_ref: str, meses_retro: int = 3
) -> dict[str, tuple[str, float]]:
    """Cota no pregão <= `data_ref` para um conjunto de CNPJs, em poucas passadas.

    Lê o Informe Diário do mês da data e retrocede até `meses_retro` meses,
    varrendo cada arquivo mensal uma única vez (eficiente para validar muitos
    candidatos de uma vez). Retorna {cnpj_normalizado: (data_iso, cota)} apenas
    para os CNPJs encontrados com pregão <= data_ref.
    """
    alvos = {_so_digitos(c) for c in cnpjs if c}
    if not alvos:
        return {}
    limite = datetime.fromisoformat(data_ref).date()
    melhor: dict[str, tuple[str, float]] = {}
    pendentes = set(alvos)

    base = limite
    for recuo in range(0, meses_retro + 1):
        if not pendentes:
            break
        ano = base.year
        mes = base.month - recuo
        while mes <= 0:
            mes += 12
            ano -= 1
        aaaamm = f"{ano:04d}{mes:02d}"
        try:
            conteudo = _baixar_informe(aaaamm)
        except ErroDadosCVM:
            continue
        try:
            with zipfile.ZipFile(io.BytesIO(conteudo)) as zf:
                for nome in (n for n in zf.namelist() if n.lower().endswith(".csv")):
                    with zf.open(nome) as bruto:
                        texto = io.TextIOWrapper(bruto, encoding="latin-1", newline="")
                        leitor = csv.DictReader(texto, delimiter=";")
                        col_cnpj = next(
                            (c for c in _COLS_CNPJ if c in (leitor.fieldnames or [])), None
                        )
                        if col_cnpj is None:
                            continue
                        for linha in leitor:
                            cnpj = _so_digitos(linha.get(col_cnpj, ""))
                            if cnpj not in alvos:
                                continue
                            data_str = (linha.get(_COL_DATA) or "").strip()
                            cota_str = (linha.get(_COL_COTA) or "").strip()
                            if not data_str or not cota_str:
                                continue
                            try:
                                d = datetime.fromisoformat(data_str).date()
                                cota = float(cota_str.replace(",", "."))
                            except ValueError:
                                continue
                            if d > limite:
                                continue
                            atual = melhor.get(cnpj)
                            if atual is None or data_str > atual[0]:
                                melhor[cnpj] = (data_str, cota)
        except zipfile.BadZipFile:
            continue
        pendentes = alvos - set(melhor)
    return melhor


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


def _cotas_mes_multi(aaaamm: str, alvos: set[str]) -> dict[str, dict[str, float]]:
    """{cnpj: {data_iso: cota}} para vários CNPJs num mês, em uma única leitura."""
    conteudo = _baixar_informe(aaaamm)
    saida: dict[str, dict[str, float]] = {c: {} for c in alvos}
    with zipfile.ZipFile(io.BytesIO(conteudo)) as zf:
        for nome in (n for n in zf.namelist() if n.lower().endswith(".csv")):
            with zf.open(nome) as bruto:
                texto = io.TextIOWrapper(bruto, encoding="latin-1", newline="")
                leitor = csv.DictReader(texto, delimiter=";")
                col_cnpj = next(
                    (c for c in _COLS_CNPJ if c in (leitor.fieldnames or [])), None
                )
                if col_cnpj is None:
                    continue
                for linha in leitor:
                    cnpj = _so_digitos(linha.get(col_cnpj, ""))
                    if cnpj not in alvos:
                        continue
                    data_str = (linha.get(_COL_DATA) or "").strip()
                    cota_str = (linha.get(_COL_COTA) or "").strip()
                    if not data_str or not cota_str:
                        continue
                    try:
                        saida[cnpj][data_str] = float(cota_str.replace(",", "."))
                    except ValueError:
                        continue
    return saida


def estimar_datas_aplicacao(
    rent_por_cnpj: dict[str, float],
    data_ref: Optional[str] = None,
    max_meses: int = 120,
) -> dict[str, dict]:
    """Estima a data de aplicação de cada fundo pela cota oficial da CVM.

    Premissa (aplicação única): a cota na compra ≈ cota_atual / (1 + rent_bruta).
    Busca no histórico do Informe Diário a data cuja cota mais se aproxima desse
    alvo, retrocedendo mês a mês (uma leitura por mês para todos os CNPJs) e
    parando cada fundo quando a série cruza o alvo.

    `rent_por_cnpj` mapeia CNPJ → rentabilidade bruta acumulada (fração). Retorna
    {cnpj: {data, cota, cota_alvo, cota_atual, data_cota_atual, desvio}} apenas
    para os fundos com estimativa possível (rent_bruta > 0 e cotas disponíveis).
    É uma ESTIMATIVA — a UI a rotula como tal e pede confirmação.
    """
    alvos = {_so_digitos(c): c for c in rent_por_cnpj if c}
    if not alvos:
        return {}
    limite = datetime.fromisoformat(data_ref).date() if data_ref else date.today()

    atuais = cotas_para_cnpjs(set(alvos), limite.isoformat())
    metas: dict[str, dict] = {}
    for cnpj_norm in list(alvos):
        rent = rent_por_cnpj[alvos[cnpj_norm]]
        info = atuais.get(cnpj_norm)
        if info is None or rent is None or rent <= 0:
            continue
        data_cota_atual, cota_atual = info
        if cota_atual <= 0:
            continue
        metas[cnpj_norm] = {
            "cota_alvo": cota_atual / (1.0 + rent),
            "cota_atual": cota_atual,
            "data_cota_atual": data_cota_atual,
            "melhor_data": None,
            "melhor_cota": None,
            "melhor_desvio": None,
            "done": False,
        }

    pendentes = set(metas)
    base = limite
    for recuo in range(0, max_meses + 1):
        if not pendentes:
            break
        ano = base.year
        mes = base.month - recuo
        while mes <= 0:
            mes += 12
            ano -= 1
        aaaamm = f"{ano:04d}{mes:02d}"
        try:
            series = _cotas_mes_multi(aaaamm, set(pendentes))
        except (ErroDadosCVM, zipfile.BadZipFile):
            continue
        for cnpj_norm in list(pendentes):
            meta = metas[cnpj_norm]
            alvo = meta["cota_alvo"]
            cruzou = False
            for data_str, cota in series.get(cnpj_norm, {}).items():
                if datetime.fromisoformat(data_str).date() > limite:
                    continue
                desvio = abs(cota - alvo)
                if meta["melhor_desvio"] is None or desvio < meta["melhor_desvio"]:
                    meta["melhor_desvio"] = desvio
                    meta["melhor_data"] = data_str
                    meta["melhor_cota"] = cota
                if cota <= alvo:
                    cruzou = True
            if cruzou:
                pendentes.discard(cnpj_norm)

    resultado: dict[str, dict] = {}
    for cnpj_norm, meta in metas.items():
        if meta["melhor_data"] is None:
            continue
        cota_alvo = meta["cota_alvo"]
        resultado[cnpj_norm] = {
            "data": meta["melhor_data"],
            "cota": meta["melhor_cota"],
            "cota_alvo": round(cota_alvo, 6),
            "cota_atual": meta["cota_atual"],
            "data_cota_atual": meta["data_cota_atual"],
            "desvio": (meta["melhor_cota"] / cota_alvo - 1.0) if cota_alvo else None,
        }
    return resultado


if __name__ == "__main__":  # smoke manual (requer rede)
    # CNPJ de exemplo; substitua por um fundo real para testar.
    print(cota_mais_recente("00.000.000/0001-00"))
