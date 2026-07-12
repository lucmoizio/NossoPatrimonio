"""Resolução automática de CNPJ de fundos pelo cadastro oficial da CVM.

O relatório da XP traz apenas o nome comercial do fundo. Para habilitar a
atualização automática de cota (cvm.py), precisamos do CNPJ. Este módulo:

1. baixa o cadastro `registro_fundo_classe.zip` da CVM (fonte oficial);
2. casa o nome comercial com a Denominação Social por similaridade de tokens;
3. CONFIRMA o candidato pela cota implícita (saldo ÷ quantidade) vs a cota
   oficial da CVM na data de referência (tolerância 0,5%) — a técnica do §8/R3.

O casamento por nome é apenas uma heurística de candidatos; a confirmação pela
cota é o que valida. Nunca gravamos um CNPJ como "validado" sem essa conferência.
"""

from __future__ import annotations

import csv
import difflib
import io
import json
import re
import time
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

from . import cvm, database

_URL_CADASTRO = "https://dados.cvm.gov.br/dados/FI/CAD/DADOS/registro_fundo_classe.zip"
_DIR_CACHE = Path(__file__).resolve().parent.parent / ".cache_cvm"
_ARQ_CACHE = _DIR_CACHE / "registro_fundo_classe.zip"
_ARQ_VALIDADOS = _DIR_CACHE / "cnpj_validados.json"
_TTL_CADASTRO = 7 * 24 * 3600  # 7 dias
_TIMEOUT = 120
_TOLERANCIA_COTA = 0.005  # 0,5%

# Tokens genéricos removidos antes de comparar nomes (não distinguem fundos).
_STOPWORDS = {
    "FUNDO", "FUNDOS", "INVESTIMENTO", "INVESTIMENTOS", "FINANCEIRO", "FINANCEIROS",
    "RENDA", "FIXA", "CREDITO", "PRIVADO", "MULTIMERCADO", "ACOES", "RESPONSABILIDADE",
    "LIMITADA", "LTDA", "COTAS", "COTA", "CLASSE", "CLASSES", "SUBCLASSE", "DE", "DA",
    "DO", "DOS", "DAS", "EM", "E", "FIC", "FICFI", "FIF", "FIRF", "RF", "CP", "LP",
    "RL", "DI", "FI", "FIM", "REFERENCIADO", "REFERENCIADA", "LONGO", "PRAZO",
    "SIMPLES", "CIC", "S.A.", "SA", "A", "I", "II", "III", "IV",
}


class ErroCadastroCVM(RuntimeError):
    """Falha ao obter/interpretar o cadastro de fundos da CVM."""


@dataclass
class Candidato:
    """Candidato de fundo no cadastro da CVM."""

    denominacao: str
    cnpj: str
    score: float


@dataclass
class ResultadoCNPJ:
    """Resultado da resolução de CNPJ para um ativo do extrato."""

    nome_extrato: str
    cnpj: Optional[str]
    denominacao: Optional[str]
    status: str  # 'validado' | 'candidato' | 'sem_cnpj' | 'fidc' | 'nao_encontrado'
    desvio_cota: Optional[float] = None
    candidatos: list[Candidato] = field(default_factory=list)

    @property
    def rotulo_status(self) -> str:
        mapa = {
            "validado": "CNPJ validado pela cota",
            "validado_cache": "CNPJ validado anteriormente pela cota",
            "candidato": "candidato (não validado pela cota)",
            "sem_cnpj": "sem CNPJ (renda fixa bancária/Tesouro)",
            "fidc": "FIDC: fora do Informe Diário FIF (R6)",
            "nao_encontrado": "não encontrado no cadastro",
        }
        return mapa.get(self.status, self.status)


# --------------------------------------------------------------------------- #
# Normalização de nomes
# --------------------------------------------------------------------------- #
def _sem_acento(texto: str) -> str:
    nfkd = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _tokens(nome: str) -> set[str]:
    """Tokens distintivos de um nome (maiúsculo, sem acento, sem stopwords)."""
    limpo = _sem_acento(nome).upper()
    limpo = re.sub(r"[^A-Z0-9 ]", " ", limpo)
    brutos = [t for t in limpo.split() if t]
    return {t for t in brutos if t not in _STOPWORDS and len(t) > 1}


# --------------------------------------------------------------------------- #
# Download e índice do cadastro
# --------------------------------------------------------------------------- #
def _baixar_cadastro() -> bytes:
    """Baixa (com cache de 7 dias) o ZIP do cadastro de fundos/classes."""
    if _ARQ_CACHE.exists() and (time.time() - _ARQ_CACHE.stat().st_mtime) < _TTL_CADASTRO:
        return _ARQ_CACHE.read_bytes()
    try:
        resp = requests.get(_URL_CADASTRO, timeout=_TIMEOUT)
        resp.raise_for_status()
        _DIR_CACHE.mkdir(parents=True, exist_ok=True)
        _ARQ_CACHE.write_bytes(resp.content)
        return resp.content
    except requests.RequestException as exc:
        if _ARQ_CACHE.exists():
            return _ARQ_CACHE.read_bytes()
        raise ErroCadastroCVM(
            f"Não foi possível baixar o cadastro de fundos da CVM e não há cache. "
            f"Detalhe: {exc}"
        ) from exc


# Índice em memória: lista de (tokens, denominacao, cnpj).
_indice_cache: Optional[list[tuple[set[str], str, str]]] = None


def carregar_indice(forcar: bool = False) -> list[tuple[set[str], str, str]]:
    """Carrega o índice (tokens, denominação, CNPJ) do cadastro de classes.

    Usa CNPJ_Classe (mesmo campo do Informe Diário). Inclui também os fundos de
    `registro_fundo.csv` como fallback (CNPJ_Fundo).
    """
    global _indice_cache
    if _indice_cache is not None and not forcar:
        return _indice_cache

    conteudo = _baixar_cadastro()
    indice: list[tuple[set[str], str, str]] = []
    with zipfile.ZipFile(io.BytesIO(conteudo)) as zf:
        for arquivo, col_cnpj in (
            ("registro_classe.csv", "CNPJ_Classe"),
            ("registro_fundo.csv", "CNPJ_Fundo"),
        ):
            if arquivo not in zf.namelist():
                continue
            with zf.open(arquivo) as bruto:
                texto = io.TextIOWrapper(bruto, encoding="latin-1", newline="")
                leitor = csv.DictReader(texto, delimiter=";")
                for linha in leitor:
                    cnpj = re.sub(r"\D", "", linha.get(col_cnpj, "") or "")
                    denom = (linha.get("Denominacao_Social") or "").strip()
                    if not cnpj or not denom:
                        continue
                    indice.append((_tokens(denom), denom, cnpj))
    _indice_cache = indice
    return indice


# --------------------------------------------------------------------------- #
# Casamento por nome
# --------------------------------------------------------------------------- #
def buscar_candidatos(nome: str, limite: int = 25) -> list[Candidato]:
    """Retorna os melhores candidatos do cadastro para um nome comercial."""
    indice = carregar_indice()
    alvo = _tokens(nome)
    if not alvo:
        return []

    pontuados: list[Candidato] = []
    alvo_txt = " ".join(sorted(alvo))
    for tokens, denom, cnpj in indice:
        if not tokens:
            continue
        inter = len(alvo & tokens)
        if inter == 0:
            continue
        overlap = inter / len(alvo)
        if overlap < 0.5:
            continue
        ratio = difflib.SequenceMatcher(None, alvo_txt, " ".join(sorted(tokens))).ratio()
        score = overlap * 0.7 + ratio * 0.3
        pontuados.append(Candidato(denominacao=denom, cnpj=cnpj, score=round(score, 4)))

    pontuados.sort(key=lambda c: c.score, reverse=True)
    # Remove CNPJs duplicados preservando o melhor score.
    vistos: set[str] = set()
    unicos: list[Candidato] = []
    for c in pontuados:
        if c.cnpj in vistos:
            continue
        vistos.add(c.cnpj)
        unicos.append(c)
        if len(unicos) >= limite:
            break
    return unicos


# --------------------------------------------------------------------------- #
# Cache de CNPJs validados por cota (reuso quando não há quantidade)
# --------------------------------------------------------------------------- #
def _chave_nome(nome: str) -> str:
    """Chave estável de um nome comercial (sem acento, maiúsculo, colapsado)."""
    return re.sub(r"\s+", " ", _sem_acento(nome).upper()).strip()


def _carregar_validados() -> dict[str, dict]:
    if not _ARQ_VALIDADOS.exists():
        return {}
    try:
        return json.loads(_ARQ_VALIDADOS.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _salvar_validado(nome: str, cnpj: str, denominacao: Optional[str]) -> None:
    dados = _carregar_validados()
    dados[_chave_nome(nome)] = {"cnpj": cnpj, "denominacao": denominacao}
    try:
        _DIR_CACHE.mkdir(parents=True, exist_ok=True)
        _ARQ_VALIDADOS.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass  # cache é best-effort


def limpar_cache_validados() -> int:
    """Apaga o cache local de CNPJs validados por cota (reimportação limpa).

    Chamado pelo reset da plataforma junto com `database.zerar_dados`.
    Retorna quantas entradas havia no cache.
    """
    if not _ARQ_VALIDADOS.exists():
        return 0
    try:
        n = len(_carregar_validados())
        _ARQ_VALIDADOS.unlink()
        return n
    except OSError:
        return 0


def _tem_cnpj_proprio(nome: str, categoria: str) -> bool:
    """Heurística: o ativo é um fundo (tem CNPJ na CVM) ou renda fixa bancária?"""
    cat = categoria.upper()
    if cat in ("CDB", "TESOURO DIRETO", "LCI/LCA", "POUPANÇA", "POUPANCA"):
        return False
    nm = _sem_acento(nome).upper()
    if nm.startswith("CDB") or "NTN-B" in nm or "NTN-F" in nm or "LTN" in nm or "TESOURO" in nm:
        return False
    return True


# --------------------------------------------------------------------------- #
# Resolução em lote (usada pela UI)
# --------------------------------------------------------------------------- #
def resolver_carteira(posicoes: list, data_ref: str) -> list[ResultadoCNPJ]:
    """Resolve o CNPJ de cada posição, confirmando pela cota implícita.

    `posicoes` é uma lista com atributos .nome, .categoria, .saldo_bruto e
    .quantidade (ex.: importador_xp.PosicaoExtraida).
    """
    resultados: list[ResultadoCNPJ] = []
    candidatos_por_indice: dict[int, list[Candidato]] = {}
    todos_cnpjs: set[str] = set()
    validados = _carregar_validados()

    for i, pos in enumerate(posicoes):
        if not _tem_cnpj_proprio(pos.nome, pos.categoria):
            resultados.append(ResultadoCNPJ(pos.nome, None, None, "sem_cnpj"))
            candidatos_por_indice[i] = []
            continue

        # Reuso: CNPJ já validado por cota anteriormente (ex.: via PDF, que tem
        # quantidade). Útil quando a fonte atual (planilha) não traz quantidade.
        cache = validados.get(_chave_nome(pos.nome))
        if cache and (pos.quantidade is None or pos.quantidade <= 0):
            resultados.append(
                ResultadoCNPJ(
                    pos.nome, cache["cnpj"], cache.get("denominacao"), "validado_cache"
                )
            )
            candidatos_por_indice[i] = []
            continue

        try:
            cands = buscar_candidatos(pos.nome)
        except ErroCadastroCVM:
            cands = []
        candidatos_por_indice[i] = cands
        todos_cnpjs.update(c.cnpj for c in cands)
        resultados.append(ResultadoCNPJ(pos.nome, None, None, "nao_encontrado", candidatos=cands))

    # Uma única leitura do Informe Diário para todos os candidatos.
    cotas: dict[str, tuple[str, float]] = {}
    if todos_cnpjs:
        try:
            cotas = cvm.cotas_para_cnpjs(todos_cnpjs, data_ref)
        except cvm.ErroDadosCVM:
            cotas = {}

    for i, pos in enumerate(posicoes):
        res = resultados[i]
        if res.status in ("sem_cnpj", "validado_cache"):
            continue
        cands = candidatos_por_indice.get(i, [])
        if not cands:
            res.status = "nao_encontrado"
            continue

        cota_implicita = (
            pos.saldo_bruto / pos.quantidade
            if pos.quantidade and pos.quantidade > 0
            else None
        )

        melhor_validado: Optional[tuple[Candidato, float]] = None
        for c in cands:
            info = cotas.get(re.sub(r"\D", "", c.cnpj))
            if info is None or cota_implicita is None:
                continue
            _data_cota, cota_oficial = info
            if cota_oficial <= 0:
                continue
            desvio = abs(cota_implicita / cota_oficial - 1.0)
            if desvio <= _TOLERANCIA_COTA and (
                melhor_validado is None or desvio < melhor_validado[1]
            ):
                melhor_validado = (c, desvio)

        if melhor_validado is not None:
            c, desvio = melhor_validado
            res.cnpj = c.cnpj
            res.denominacao = c.denominacao
            res.status = "validado"
            res.desvio_cota = round(desvio, 5)
            _salvar_validado(pos.nome, c.cnpj, c.denominacao)
        else:
            # Sem confirmação por cota: melhor candidato por nome fica como sugestão.
            melhor = cands[0]
            res.cnpj = melhor.cnpj
            res.denominacao = melhor.denominacao
            res.desvio_cota = None
            nm = _sem_acento(pos.nome).upper()
            res.status = "fidc" if "FIDC" in nm else "candidato"

    return resultados


# --------------------------------------------------------------------------- #
# Sincronização do cadastro (classe/tipo) para comparação por pares
# --------------------------------------------------------------------------- #
def _classe_de(linha: dict) -> str:
    """Classe/categoria de comparação: ANBIMA > Classificação CVM > Tipo_Classe.

    O cadastro da CVM não traz taxa de administração; por isso `taxa_adm` fica
    None (limitação oficial documentada no README).
    """
    for col in ("Classificacao_Anbima", "Classificacao", "Tipo_Classe"):
        valor = (linha.get(col) or "").strip()
        if valor:
            return valor
    return ""


def sincronizar_cadastro_fundos(caminho=None) -> int:
    """Popula `fundos_cadastro` (cnpj, denominação, classe, tipo, situação).

    Lê `registro_classe.csv` (CNPJ_Classe, mesmo id do Informe Diário) e usa
    `registro_fundo.csv` (CNPJ_Fundo/Tipo_Fundo) como complemento. Retorna o
    número de registros gravados. Fonte oficial e gratuita (CVM).
    """
    conteudo = _baixar_cadastro()
    registros: list[dict] = []
    with zipfile.ZipFile(io.BytesIO(conteudo)) as zf:
        nomes = zf.namelist()
        # Complemento primeiro (será sobrescrito pela classe quando o CNPJ coincidir).
        if "registro_fundo.csv" in nomes:
            with zf.open("registro_fundo.csv") as bruto:
                texto = io.TextIOWrapper(bruto, encoding="latin-1", newline="")
                for linha in csv.DictReader(texto, delimiter=";"):
                    cnpj = re.sub(r"\D", "", linha.get("CNPJ_Fundo", "") or "")
                    if not cnpj:
                        continue
                    tipo = (linha.get("Tipo_Fundo") or "").strip()
                    registros.append(
                        {
                            "cnpj": cnpj,
                            "denominacao": (linha.get("Denominacao_Social") or "").strip(),
                            "classe": tipo,
                            "tipo": tipo,
                            "taxa_adm": None,
                            "situacao": (linha.get("Situacao") or "").strip(),
                        }
                    )
        # Primário: classes (CNPJ_Classe é o CNPJ que aparece no Informe Diário).
        if "registro_classe.csv" in nomes:
            with zf.open("registro_classe.csv") as bruto:
                texto = io.TextIOWrapper(bruto, encoding="latin-1", newline="")
                for linha in csv.DictReader(texto, delimiter=";"):
                    cnpj = re.sub(r"\D", "", linha.get("CNPJ_Classe", "") or "")
                    if not cnpj:
                        continue
                    registros.append(
                        {
                            "cnpj": cnpj,
                            "denominacao": (linha.get("Denominacao_Social") or "").strip(),
                            "classe": _classe_de(linha),
                            "tipo": (linha.get("Tipo_Classe") or "").strip(),
                            "taxa_adm": None,
                            "situacao": (linha.get("Situacao") or "").strip(),
                        }
                    )
    return database.upsert_fundos_cadastro(registros, caminho=caminho)


def formatar_cnpj(cnpj: Optional[str]) -> Optional[str]:
    """Formata 14 dígitos como 00.000.000/0000-00 (ou devolve como está)."""
    if not cnpj:
        return cnpj
    d = re.sub(r"\D", "", cnpj)
    if len(d) != 14:
        return cnpj
    return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"


if __name__ == "__main__":  # smoke manual (requer rede)
    from . import importador_xp
    import sys

    if len(sys.argv) > 1:
        res = importador_xp.importar(sys.argv[1])
        resolvidos = resolver_carteira(res.posicoes, res.metadados.data_referencia)
        for r in resolvidos:
            print(f"{r.nome_extrato[:45]:45} -> {formatar_cnpj(r.cnpj)} [{r.rotulo_status}]")
