"""Parser da Posição Detalhada da XP exportada em planilha (XLSX/CSV).

Esse export é mais rico e confiável que o PDF: além de nome e saldo bruto,
traz o **valor aplicado** (custo de aquisição), a **data de aplicação** e a
**quantidade** (nas seções de renda fixa) — dados ausentes no relatório de
performance em PDF.

Layout observado (planilha "Sua carteira"):
    - Cabeçalho: "Conta: <n> | dd/mm/aaaa, hh:mm"; titular; total investido.
    - Seções por classe: "Fundos de Investimentos", "COE", "Renda Fixa",
      "Tesouro Direto", etc., com subtotal na última coluna.
    - Sub-cabeçalhos por estratégia: "<pct> | <Estratégia>" seguidos das
      colunas daquela seção (variam por seção — por isso lemos os nomes das
      colunas dinamicamente).
    - Linhas de ativo: nome + colunas conforme o sub-cabeçalho vigente.

Nenhum dado é inventado: campos ausentes ficam None.
"""

from __future__ import annotations

import io
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

import pandas as pd

from .importador_xp import (
    ErroImportacaoXP,
    MetadadosExtrato,
    PosicaoExtraida,
    ResultadoImportacao,
    mapear_categoria,
)

FontePlanilha = Union[str, Path, bytes, bytearray]

# Nomes de seção (classe) reconhecidos na coluna 0.
_SECOES = {
    "FUNDOS DE INVESTIMENTOS": "Fundos",
    "COE": "COE",
    "RENDA FIXA": "Renda Fixa",
    "TESOURO DIRETO": "Tesouro Direto",
    "ACOES": "Ações",
    "FUNDOS IMOBILIARIOS": "Fundos Imobiliários",
    "PREVIDENCIA": "Previdência",
}

# Marcadores de coluna de saldo bruto (variam por seção).
_COLS_SALDO = ("posição", "posicao", "posição na taxa de compra", "posicao na taxa de compra")


def _norm(texto: object) -> str:
    import unicodedata

    s = "" if texto is None else str(texto)
    nfkd = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip()


def _valor(x: object) -> Optional[float]:
    """'R$ 53.240,87' → 53240.87; '30,79%' → None (não é dinheiro)."""
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    s = str(x).strip()
    if not s or "%" in s:
        return None
    s = s.replace("R$", "").replace("\xa0", "").strip()
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _data_iso(x: object) -> Optional[str]:
    """'22/03/2024' → '2024-03-22'."""
    if x is None:
        return None
    s = str(x).strip()
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", s)
    if not m:
        return None
    d, mes, a = m.groups()
    return f"{a}-{mes}-{d}"


def _pct(x: object) -> Optional[float]:
    if x is None:
        return None
    s = str(x).strip()
    if "%" not in s:
        return None
    try:
        return float(s.replace("%", "").replace(".", "").replace(",", ".")) / 100.0
    except ValueError:
        return None


def _ler_planilha(fonte: FontePlanilha, nome_arquivo: Optional[str]) -> pd.DataFrame:
    """Lê o arquivo (xlsx ou csv) como DataFrame sem cabeçalho."""
    nome = (nome_arquivo or (fonte if isinstance(fonte, (str, Path)) else "")) or ""
    nome = str(nome).lower()
    dados = io.BytesIO(fonte) if isinstance(fonte, (bytes, bytearray)) else fonte

    if nome.endswith(".csv"):
        for sep in (";", ",", "\t"):
            try:
                if isinstance(dados, io.BytesIO):
                    dados.seek(0)
                df = pd.read_csv(dados, header=None, sep=sep, dtype=str, encoding="latin-1")
                if df.shape[1] >= 3:
                    return df
            except Exception:
                continue
        raise ErroImportacaoXP("Não foi possível ler o CSV da posição detalhada.")

    try:
        return pd.read_excel(dados, sheet_name=0, header=None, dtype=object)
    except Exception as exc:
        raise ErroImportacaoXP(f"Não foi possível ler a planilha: {exc}") from exc


def extrair_metadados_planilha(df: pd.DataFrame) -> MetadadosExtrato:
    """Extrai conta, data de referência e total investido do cabeçalho."""
    texto = "\n".join(
        " ".join("" if pd.isna(v) else str(v) for v in row) for _, row in df.head(6).iterrows()
    )
    conta = None
    data_iso = None
    m = re.search(r"Conta:\s*(\d+)\s*\|\s*(\d{2}/\d{2}/\d{4})", texto)
    if m:
        conta = m.group(1)
        data_iso = _data_iso(m.group(2))

    total = None
    # "Total investido" costuma estar na linha seguinte ao rótulo.
    for i, row in df.head(6).iterrows():
        vals = ["" if pd.isna(v) else str(v) for v in row]
        if any("Total investido" in _norm(v) for v in vals):
            # procura o primeiro valor monetário na próxima linha
            if i + 1 < len(df):
                for v in df.iloc[i + 1]:
                    total = _valor(v)
                    if total is not None:
                        break
            break
    return MetadadosExtrato(conta=conta, data_referencia=data_iso, patrimonio_total=total)


def _mapear_indices(cabecalho: list[str]) -> dict[str, int]:
    """Mapeia papéis de coluna a partir dos nomes do sub-cabeçalho da seção."""
    idx: dict[str, int] = {}
    for j, nome in enumerate(cabecalho):
        n = _norm(nome).lower()
        if n in _COLS_SALDO and "saldo" not in idx:
            idx["saldo"] = j
        elif n == "valor aplicado" and "valor_aplicado" not in idx:
            idx["valor_aplicado"] = j
        elif n.startswith("data aplica"):
            idx["data_aplicacao"] = j
        elif n == "quantidade":
            idx["quantidade"] = j
        elif n in ("% alocacao", "% alocação"):
            idx["pct"] = j
        elif n in ("rentabilidade bruta", "rentabilidade"):
            idx["rent_bruta"] = j
    return idx


def extrair_posicoes_planilha(df: pd.DataFrame) -> list[PosicaoExtraida]:
    """Percorre a planilha e extrai as posições, seção a seção."""
    posicoes: list[PosicaoExtraida] = []
    secao_atual = ""
    estrategia_atual = ""
    indices: dict[str, int] = {}

    for _, row in df.iterrows():
        celulas = list(row)
        col0 = _norm(celulas[0]) if celulas else ""
        if not col0:
            continue

        # Seção (classe): col0 é um nome de seção e as colunas do meio vazias.
        chave_secao = _SECOES.get(col0.upper())
        if chave_secao is not None:
            secao_atual = chave_secao
            estrategia_atual = ""
            indices = {}
            continue

        # Sub-cabeçalho de estratégia: "<pct> | <Estratégia>" + col1 de saldo.
        col1 = _norm(celulas[1]).lower() if len(celulas) > 1 else ""
        if "|" in col0 and col1 in _COLS_SALDO:
            estrategia_atual = col0.split("|", 1)[1].strip()
            cabecalho = [_norm(c) for c in celulas]
            indices = _mapear_indices(cabecalho)
            continue

        # Linha de ativo: precisa ter a coluna de saldo mapeada e um valor nela.
        if "saldo" not in indices:
            continue
        saldo = _valor(celulas[indices["saldo"]]) if indices["saldo"] < len(celulas) else None
        if saldo is None:
            continue

        def _campo(chave: str) -> object:
            j = indices.get(chave)
            return celulas[j] if (j is not None and j < len(celulas)) else None

        nome = _norm(celulas[0])
        quantidade = _valor(_campo("quantidade"))
        categoria = _categoria_por_secao(secao_atual, estrategia_atual, nome)
        posicoes.append(
            PosicaoExtraida(
                nome=nome,
                estrategia=estrategia_atual or secao_atual,
                saldo_bruto=round(saldo, 2),
                quantidade=quantidade,
                pct_alocacao=_pct(_campo("pct")),
                categoria=categoria,
                valor_aplicado=(lambda v: round(v, 2) if v is not None else None)(_valor(_campo("valor_aplicado"))),
                data_aplicacao=_data_iso(_campo("data_aplicacao")),
                rent_bruta=_pct(_campo("rent_bruta")),
            )
        )

    if not posicoes:
        raise ErroImportacaoXP(
            "Nenhum ativo encontrado na planilha. Confirme que é a 'Posição "
            "Detalhada' exportada pela XP."
        )
    return posicoes


def _categoria_por_secao(secao: str, estrategia: str, nome: str) -> str:
    """Deriva a categoria considerando também a seção da planilha."""
    if secao == "COE":
        return "COE"
    if secao == "Tesouro Direto":
        return "Tesouro Direto"
    if secao == "Fundos Imobiliários":
        return "Fundo Imobiliário"
    if secao == "Ações":
        return "Ações"
    # Fundos e Renda Fixa: reaproveita a heurística por estratégia/nome.
    return mapear_categoria(estrategia, nome)


def importar(
    fonte: FontePlanilha, nome_arquivo: Optional[str] = None, tolerancia: float = 1.0
) -> ResultadoImportacao:
    """Faz o parse completo da planilha e confere a soma contra o total."""
    df = _ler_planilha(fonte, nome_arquivo)
    meta = extrair_metadados_planilha(df)
    posicoes = extrair_posicoes_planilha(df)
    soma = round(sum(p.saldo_bruto for p in posicoes), 2)
    confere = (
        meta.patrimonio_total is not None
        and abs(soma - meta.patrimonio_total) <= tolerancia
    )
    return ResultadoImportacao(
        metadados=meta, posicoes=posicoes, soma_saldos=soma, confere_total=confere
    )


if __name__ == "__main__":  # smoke manual
    import sys

    if len(sys.argv) > 1:
        res = importar(sys.argv[1], sys.argv[1])
        print(f"Conta {res.metadados.conta} | Ref {res.metadados.data_referencia} | Total {res.metadados.patrimonio_total}")
        print(f"Ativos: {len(res.posicoes)} | Soma: {res.soma_saldos} | Confere: {res.confere_total}")
        for p in res.posicoes:
            print(f"  - [{p.estrategia}] {p.nome[:48]:48} bruto={p.saldo_bruto} aplic={p.valor_aplicado} data={p.data_aplicacao} qtd={p.quantidade} -> {p.categoria}")
