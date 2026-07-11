"""Parser do relatório de performance da XP (XPerformance, PDF).

Extrai a seção "POSIÇÃO DETALHADA DOS ATIVOS" — nome, saldo bruto, quantidade
de cotas e %alocação de cada ativo — além dos metadados do cabeçalho (conta,
data de referência, patrimônio total bruto).

Nenhum dado é inventado: o que não está no relatório (CNPJ, custo de aquisição,
data da 1ª aplicação, taxa de administração) fica a cargo da resolução via CVM
(cadastro_cvm.py) ou do preenchimento do usuário na tela de conferência.
"""

from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

FontesPDF = Union[str, Path, bytes, bytearray, io.IOBase]

# Estratégias que aparecem como subtotais na tabela (não são ativos).
ESTRATEGIAS = {
    "POS FIXADO": "Pós Fixado",
    "INFLACAO": "Inflação",
    "PRE FIXADO": "Pré Fixado",
    "CAIXA": "Caixa",
    "PROVENTOS": "Proventos",
}

_RE_DINHEIRO = re.compile(r"R\$\s*(\d{1,3}(?:\.\d{3})*,\d{2})")
_RE_NUMERO = re.compile(r"^-?\d[\d.]*$")
_RE_PCT = re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d+%$")
_RE_DATA = re.compile(r"(\d{2}/\d{2}/\d{4})")
_RE_SO_NUMERO = re.compile(r"^\d{1,3}$")

# Linhas de cabeçalho/rodapé/seção a ignorar quando montando o nome do ativo.
_LIXO_NOME = (
    "RELATORIO INFORMATIVO",
    "OF 10",
    "POSICAO DETALHADA",
    "PRECIFICACAO",
    "ESTRATEGIA SALDO",
    "MES ATUAL",
    "24 MESES",
    "DATA DE REFERENCIA",
    "SALDO BRUTO",
)


class ErroImportacaoXP(RuntimeError):
    """Falha ao interpretar o PDF do relatório XP."""


@dataclass
class PosicaoExtraida:
    """Uma linha de ativo extraída da posição detalhada.

    Os campos `valor_aplicado`, `data_aplicacao` e `rent_bruta` só são
    preenchidos pelo importador de planilha (o relatório em PDF não os traz).
    """

    nome: str
    estrategia: str
    saldo_bruto: float
    quantidade: Optional[float]
    pct_alocacao: Optional[float]
    categoria: str
    valor_aplicado: Optional[float] = None
    data_aplicacao: Optional[str] = None
    rent_bruta: Optional[float] = None


@dataclass
class MetadadosExtrato:
    """Cabeçalho do relatório."""

    conta: Optional[str]
    data_referencia: Optional[str]  # ISO yyyy-mm-dd
    patrimonio_total: Optional[float]


@dataclass
class ResultadoImportacao:
    """Resultado completo do parse: metadados + posições + conferência."""

    metadados: MetadadosExtrato
    posicoes: list[PosicaoExtraida]
    soma_saldos: float
    confere_total: bool


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def _sem_acento(texto: str) -> str:
    nfkd = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _normalizar(texto: str) -> str:
    """Maiúsculas, sem acento e com espaços colapsados (para comparações)."""
    return re.sub(r"\s+", " ", _sem_acento(texto).upper()).strip()


def _valor_brl(texto: str) -> float:
    """'45.981,44' → 45981.44."""
    return float(texto.replace(".", "").replace(",", "."))


def _num(texto: str) -> Optional[float]:
    """Converte quantidade/número da XP em float.

    A XP usa ponto como separador decimal na quantidade ('14253.56', '25.02').
    """
    texto = texto.strip()
    if texto in ("-", "", "–"):
        return None
    try:
        return float(texto)
    except ValueError:
        return None


def _pct(texto: str) -> Optional[float]:
    """'93,47%' → 0.9347 (fração)."""
    try:
        return _valor_brl(texto.replace("%", "").strip()) / 100.0
    except ValueError:
        return None


def _texto_paginas(pdf: FontesPDF) -> list[str]:
    """Extrai o texto de cada página do PDF via pdfplumber."""
    import pdfplumber

    if isinstance(pdf, (bytes, bytearray)):
        origem: object = io.BytesIO(pdf)
    elif isinstance(pdf, io.IOBase):
        origem = pdf
    else:
        origem = str(pdf)

    paginas: list[str] = []
    with pdfplumber.open(origem) as doc:
        for pag in doc.pages:
            paginas.append(pag.extract_text() or "")
    return paginas


# --------------------------------------------------------------------------- #
# Mapeamento de categoria
# --------------------------------------------------------------------------- #
def mapear_categoria(estrategia: str, nome: str) -> str:
    """Deriva a categoria (lista fechada) a partir da estratégia e do nome."""
    est = _normalizar(estrategia)
    nm = _normalizar(nome)

    if est == "INFLACAO":
        if "NTN-B" in nm or "TESOURO" in nm:
            return "Tesouro Direto"
        if nm.startswith("CDB"):
            return "CDB"
        return "Renda Fixa - Inflação"

    if est == "PRE FIXADO":
        if "TESOURO" in nm or "LTN" in nm or "NTN-F" in nm:
            return "Tesouro Direto"
        if nm.startswith("CDB"):
            return "CDB"
        return "Renda Fixa - Prefixado"

    # Pós-fixado (e demais casos).
    if "FIDC" in nm:
        return "FIDC"
    if nm.startswith("CDB"):
        return "CDB"
    if "LCI" in nm or "LCA" in nm:
        return "LCI/LCA"
    if "CP" in nm.split() or "CREDITO" in nm:
        return "Crédito Privado"
    return "Renda Fixa - Pós-fixado"


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _linhas_secao_posicao(paginas: list[str]) -> list[str]:
    """Retorna as linhas pertencentes à seção de posição detalhada.

    A seção começa na primeira página cujo conteúdo normalizado contém
    'POSICAO DETALHADA DOS ATIVOS' e termina ao encontrar 'MOVIMENTACOES'.
    """
    linhas: list[str] = []
    coletando = False
    for texto in paginas:
        norm_pag = _normalizar(texto)
        if "POSICAO DETALHADA DOS ATIVOS" in norm_pag:
            coletando = True
        if coletando:
            if "MOVIMENTACOES DO MES" in norm_pag or "MOVIMENTACOES\n" in (texto.upper()):
                # coleta o que veio antes do bloco de movimentações e encerra
                for linha in texto.splitlines():
                    if "MOVIMENTA" in _normalizar(linha):
                        break
                    linhas.append(linha)
                break
            linhas.extend(texto.splitlines())
    return linhas


def _eh_lixo(linha: str) -> bool:
    norm = _normalizar(linha)
    if not norm:
        return True
    return any(marca in norm for marca in _LIXO_NOME)


def extrair_posicoes(pdf: FontesPDF) -> list[PosicaoExtraida]:
    """Extrai a lista de ativos da posição detalhada do relatório."""
    paginas = _texto_paginas(pdf)
    linhas = _linhas_secao_posicao(paginas)
    if not linhas:
        raise ErroImportacaoXP(
            "Seção 'POSIÇÃO DETALHADA DOS ATIVOS' não encontrada no PDF. "
            "Confirme que é um relatório XPerformance."
        )

    posicoes: list[PosicaoExtraida] = []
    estrategia_atual = ""
    ultimo_ativo: Optional[PosicaoExtraida] = None
    # Fragmentos de nome sem valor acumulados desde a última linha de valor.
    # No layout do pdfplumber, nomes longos (CDB, NTN-B) quebram em linhas
    # separadas do valor: parte pode vir ANTES da linha do R$ (antes vazio) e
    # parte DEPOIS (continuação do ativo já lido).
    pendentes: list[str] = []

    def _anexar_ao_ultimo() -> None:
        if ultimo_ativo is not None and pendentes:
            ultimo_ativo.nome = re.sub(
                r"\s+", " ", (ultimo_ativo.nome + " " + " ".join(pendentes)).strip()
            )
            ultimo_ativo.categoria = mapear_categoria(
                ultimo_ativo.estrategia, ultimo_ativo.nome
            )

    for linha in linhas:
        m = _RE_DINHEIRO.search(linha)
        if not m:
            fragmento = linha.strip()
            if fragmento and not _eh_lixo(linha) and not _RE_SO_NUMERO.match(fragmento):
                pendentes.append(fragmento)
            continue

        # Rejeita linhas com múltiplos valores em R$ (ex.: tabela de evolução).
        if len(_RE_DINHEIRO.findall(linha)) != 1:
            pendentes.clear()
            continue

        antes = linha[: m.start()].strip()
        saldo = _valor_brl(m.group(1))
        resto = linha[m.end():].split()
        primeiro = resto[0] if resto else ""

        # Linha de subtotal por estratégia: quantidade "-" ou nome de estratégia.
        norm_antes = _normalizar(antes)
        chave_estrategia = next(
            (v for k, v in ESTRATEGIAS.items() if norm_antes == k or norm_antes.startswith(k + " ")),
            None,
        )
        if primeiro in ("-", "–") or chave_estrategia is not None:
            _anexar_ao_ultimo()  # continuação do último ativo antes do subtotal
            pendentes.clear()
            if chave_estrategia:
                estrategia_atual = chave_estrategia
            ultimo_ativo = None
            continue

        quantidade = _num(primeiro) if _RE_NUMERO.match(primeiro) else None
        pct_aloc = next((_pct(t) for t in resto if t.endswith("%")), None)

        if antes:
            # Ativo autocontido nesta linha. Fragmentos pendentes eram, na
            # verdade, continuação do ativo ANTERIOR.
            _anexar_ao_ultimo()
            pendentes.clear()
            nome = antes
        else:
            # Linha só de valor: o nome veio nas linhas anteriores (pendentes).
            nome = " ".join(pendentes).strip()
            pendentes.clear()

        if not nome:
            continue

        ativo = PosicaoExtraida(
            nome=re.sub(r"\s+", " ", nome),
            estrategia=estrategia_atual,
            saldo_bruto=saldo,
            quantidade=quantidade,
            pct_alocacao=pct_aloc,
            categoria=mapear_categoria(estrategia_atual, nome),
        )
        posicoes.append(ativo)
        ultimo_ativo = ativo

    _anexar_ao_ultimo()  # flush de continuação ao final da seção

    if not posicoes:
        raise ErroImportacaoXP("Nenhum ativo pôde ser extraído da posição detalhada.")
    return posicoes


def extrair_metadados(pdf: FontesPDF) -> MetadadosExtrato:
    """Extrai conta, data de referência e patrimônio total bruto do cabeçalho."""
    paginas = _texto_paginas(pdf)
    texto = "\n".join(paginas)

    # Data de referência.
    data_iso: Optional[str] = None
    m_ref = re.search(r"Refer[êe]ncia[^\d]*(\d{2}/\d{2}/\d{4})", texto, re.IGNORECASE)
    if not m_ref:
        m_ref = _RE_DATA.search(texto)
    if m_ref:
        try:
            data_iso = datetime.strptime(m_ref.group(1), "%d/%m/%Y").date().isoformat()
        except ValueError:
            data_iso = None

    # Conta (sequência de dígitos após 'Conta').
    conta: Optional[str] = None
    m_conta = re.search(r"Conta[\s\S]{0,40}?(\d{4,})", texto)
    if m_conta:
        conta = m_conta.group(1)

    # Patrimônio total bruto (o rótulo e o valor podem estar em linhas separadas).
    total: Optional[float] = None
    m_tot = re.search(
        r"PATRIM\wNIO TOTAL BRUTO.*?R\$\s*([\d.]+,\d{2})",
        _sem_acento(texto),
        re.IGNORECASE | re.DOTALL,
    )
    if m_tot:
        total = _valor_brl(m_tot.group(1))

    return MetadadosExtrato(conta=conta, data_referencia=data_iso, patrimonio_total=total)


def importar(pdf: FontesPDF, tolerancia: float = 0.02) -> ResultadoImportacao:
    """Faz o parse completo do relatório e confere a soma contra o total."""
    meta = extrair_metadados(pdf)
    posicoes = extrair_posicoes(pdf)
    soma = round(sum(p.saldo_bruto for p in posicoes), 2)
    confere = (
        meta.patrimonio_total is not None
        and abs(soma - meta.patrimonio_total) <= tolerancia
    )
    return ResultadoImportacao(
        metadados=meta,
        posicoes=posicoes,
        soma_saldos=soma,
        confere_total=confere,
    )


if __name__ == "__main__":  # smoke manual
    import sys

    if len(sys.argv) > 1:
        res = importar(sys.argv[1])
        print(f"Conta: {res.metadados.conta} | Ref: {res.metadados.data_referencia}")
        print(f"Patrimônio total: {res.metadados.patrimonio_total}")
        print(f"Ativos: {len(res.posicoes)} | Soma: {res.soma_saldos} | Confere: {res.confere_total}")
        for p in res.posicoes:
            print(f"  - [{p.estrategia}] {p.nome}: {p.saldo_bruto} (qtd {p.quantidade}) -> {p.categoria}")
