"""Parser (best-effort) do Extrato de Movimentações da XP.

Objetivo: descobrir a **data da primeira aplicação** de cada ativo — informação
que a "Posição Detalhada" não traz, mas que existe no extrato de movimentações
(aplicações/compras/aportes com data). Aceita XLSX/CSV (preferível) e PDF.

O layout do extrato varia entre exportações, então o parser é **heurístico**:
em cada linha procura (1) uma data dd/mm/aaaa, (2) um tipo de movimentação de
ENTRADA (aplicação, compra, aporte, subscrição) e (3) o nome do ativo (o maior
trecho de texto que sobra depois de remover data, valores e o tipo). Guarda a
MENOR data por ativo. Nada é inventado: linhas sem esses três elementos são
ignoradas.

Por ser heurístico, a UI mostra o que foi lido e casa com os ativos já
cadastrados (por similaridade de nome) para você conferir antes de gravar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional, Union

import pandas as pd

from .importador_xp import ErroImportacaoXP, _texto_paginas
from .importador_xp_planilha import _data_iso, _ler_planilha, _norm

Fonte = Union[str, Path, bytes, bytearray]

# Tipos de movimentação que representam ENTRADA de recurso no ativo.
_TIPOS_ENTRADA = (
    "APLICACAO",
    "COMPRA",
    "APORTE",
    "SUBSCRICAO",
    "ENTRADA",
    "INVESTIMENTO",
)
# Tipos que NÃO são entrada (evita casar "resgate"/"venda" por engano).
_TIPOS_SAIDA = ("RESGATE", "VENDA", "SAIDA", "AMORTIZACAO", "COME-COTAS", "COME COTAS")

_RE_DATA = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")
_RE_MOEDA = re.compile(r"R\$\s*[\d\.\,]+")
_RE_NUM = re.compile(r"[-+]?\d[\d\.\,]*%?")


@dataclass
class AplicacaoDetectada:
    """Primeira aplicação detectada para um ativo no extrato de movimentações."""

    nome: str
    data: str  # ISO (yyyy-mm-dd) — a menor data de entrada encontrada
    ocorrencias: int


def _linhas_texto(fonte: Fonte, nome_arquivo: Optional[str]) -> list[str]:
    """Reduz o arquivo (planilha ou PDF) a uma lista de linhas de texto."""
    nome = (nome_arquivo or (fonte if isinstance(fonte, (str, Path)) else "")) or ""
    if str(nome).lower().endswith(".pdf"):
        paginas = _texto_paginas(fonte)  # type: ignore[arg-type]
        linhas: list[str] = []
        for pag in paginas:
            linhas.extend(l for l in pag.splitlines() if l.strip())
        return linhas
    df = _ler_planilha(fonte, nome_arquivo)
    linhas = []
    for _, row in df.iterrows():
        celulas = ["" if pd.isna(v) else str(v) for v in row]
        texto = " | ".join(c for c in celulas if c.strip())
        if texto.strip():
            linhas.append(texto)
    return linhas


def _eh_entrada(texto_norm: str) -> bool:
    if any(s in texto_norm for s in _TIPOS_SAIDA):
        return False
    return any(t in texto_norm for t in _TIPOS_ENTRADA)


def _nome_da_linha(texto: str) -> str:
    """Extrai um nome de ativo plausível removendo datas, valores e o tipo."""
    s = _RE_DATA.sub(" ", texto)
    s = _RE_MOEDA.sub(" ", s)
    s = _RE_NUM.sub(" ", s)
    for t in _TIPOS_ENTRADA + _TIPOS_SAIDA:
        s = re.sub(t, " ", s, flags=re.IGNORECASE)
    # Remove rótulos de coluna comuns e separadores.
    s = re.sub(r"\b(data|movimenta[cç][aã]o|produto|ativo|quantidade|valor|conta|tipo)\b", " ", s, flags=re.IGNORECASE)
    partes = [p.strip(" |-–\t") for p in s.split("|")]
    partes = [p for p in partes if len(p) >= 4 and any(ch.isalpha() for ch in p)]
    if not partes:
        return ""
    return max(partes, key=len).strip()


def extrair_aplicacoes(fonte: Fonte, nome_arquivo: Optional[str] = None) -> list[AplicacaoDetectada]:
    """Lê o extrato e retorna a primeira aplicação detectada por ativo."""
    linhas = _linhas_texto(fonte, nome_arquivo)
    agregado: dict[str, dict] = {}  # chave normalizada -> {nome, data, ocorrencias}
    for linha in linhas:
        norm = _norm(linha).upper()
        if not _eh_entrada(norm):
            continue
        m = _RE_DATA.search(linha)
        if not m:
            continue
        data_iso = _data_iso(m.group(1))
        if not data_iso:
            continue
        nome = _nome_da_linha(linha)
        if not nome:
            continue
        chave = _norm(nome).upper()
        atual = agregado.get(chave)
        if atual is None:
            agregado[chave] = {"nome": nome, "data": data_iso, "ocorrencias": 1}
        else:
            atual["ocorrencias"] += 1
            if data_iso < atual["data"]:
                atual["data"] = data_iso

    resultado = [
        AplicacaoDetectada(v["nome"], v["data"], v["ocorrencias"]) for v in agregado.values()
    ]
    if not resultado:
        raise ErroImportacaoXP(
            "Nenhuma aplicação com data foi reconhecida. Confirme que é o 'Extrato "
            "de Movimentações' da XP (de preferência em Excel)."
        )
    resultado.sort(key=lambda a: a.data)
    return resultado


def casar_com_ativos(
    aplicacoes: list[AplicacaoDetectada], ativos: list, limiar: float = 0.6
) -> list[dict]:
    """Casa as aplicações detectadas com ativos cadastrados por similaridade.

    `ativos` é uma lista de linhas com 'id', 'nome' e 'data_aplicacao'. Retorna
    propostas {id, ativo, data_atual, data_detectada, score, origem} para os
    ativos que tiveram correspondência acima de `limiar`.
    """
    propostas: list[dict] = []
    for a in ativos:
        alvo = _norm(a["nome"]).upper()
        melhor = None
        melhor_score = 0.0
        for ap in aplicacoes:
            score = SequenceMatcher(None, alvo, _norm(ap.nome).upper()).ratio()
            if score > melhor_score:
                melhor_score = score
                melhor = ap
        if melhor is not None and melhor_score >= limiar:
            propostas.append(
                {
                    "id": int(a["id"]),
                    "ativo": a["nome"],
                    "data_atual": a["data_aplicacao"] or "",
                    "data_detectada": melhor.data,
                    "origem": melhor.nome,
                    "score": round(melhor_score, 2),
                }
            )
    return propostas


if __name__ == "__main__":  # smoke manual
    import sys

    if len(sys.argv) > 1:
        for ap in extrair_aplicacoes(sys.argv[1], sys.argv[1]):
            print(f"{ap.data}  x{ap.ocorrencias:<3}  {ap.nome}")
