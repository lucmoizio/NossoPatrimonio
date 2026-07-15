"""Parser do Extrato da conta investimento da XP (XLSX).

Layout observado (aba única, ex. "Planilha1"):
    - Cabeçalho: "Extrato da conta", conta XP, titular, período declarado.
    - Tabela: Movimentação | Liquidação | Lançamento | Valor (R$) | Saldo (R$).

Diferente da Posição Detalhada (snapshot) e do extrato de movimentações (heurístico):
aqui as datas vêm em colunas dedicadas e os lançamentos seguem padrões estáveis
(COMPRA, RESGATE, TED APLICAÇÃO FUNDOS, Pgto Juros, etc.).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional, Union

import pandas as pd

from .importador_movimentacoes_xp import AplicacaoDetectada, casar_com_ativos
from .importador_xp import ErroImportacaoXP
from .importador_xp_planilha import _data_iso, _ler_planilha, _norm, _valor

Fonte = Union[str, Path, bytes, bytearray]

_TIPOS_ENTRADA = frozenset({"aplic_fundo", "compra"})
_TIPOS_SAIDA = frozenset({"resgate"})
_TIPOS_PROVENTO = frozenset({"juros"})

_RE_CONTA = re.compile(r"Conta\s*XP:\s*(\d+)", re.I)
_RE_PERIODO = re.compile(
    r"De:\s*(\d{2}/\d{2}/\d{4})\s*At[eé]:\s*(\d{2}/\d{2}/\d{4})", re.I
)
_RE_CODIGO_COE = re.compile(r"\b(XP[A-Z0-9]{6,})\b", re.I)
_RE_FUNDOS = re.compile(r"TED\s+APLICA[ÇC][AÃ]O\s+FUNDOS\s+(.+)$", re.I)
_RE_JUROS = re.compile(
    r"Pgto\s+Juros\s+([A-Z0-9]+)\s*\|\s*(.+)$", re.I
)


@dataclass
class MetadadosExtratoConta:
    conta: Optional[str] = None
    titular: Optional[str] = None
    periodo_de: Optional[str] = None  # ISO
    periodo_ate: Optional[str] = None  # ISO
    data_consulta: Optional[str] = None  # ISO
    periodo_real_de: Optional[str] = None
    periodo_real_ate: Optional[str] = None


@dataclass
class LinhaExtratoConta:
    data_mov: str
    data_liq: Optional[str]
    lancamento: str
    valor: float
    saldo: Optional[float]
    tipo: str
    ativo_nome: Optional[str] = None
    codigo_ativo: Optional[str] = None

    def import_hash(self, conta: Optional[str]) -> str:
        base = (
            f"{conta or ''}|{self.data_mov}|{self.data_liq or ''}|"
            f"{self.lancamento}|{self.valor:.2f}"
        )
        return hashlib.sha256(base.encode()).hexdigest()[:32]


@dataclass
class ProventoProposto:
    nome: str
    codigo: Optional[str]
    data: str
    valor: float
    lancamento: str


@dataclass
class AplicacaoExtrato:
    """Primeira aplicação detectada no extrato da conta (com código opcional)."""

    nome: str
    data: str
    ocorrencias: int
    codigo: Optional[str] = None


@dataclass
class ExtratoContaResult:
    metadados: MetadadosExtratoConta
    linhas: list[LinhaExtratoConta] = field(default_factory=list)

    @property
    def primeira_aplicacao_por_ativo(self) -> list[AplicacaoExtrato]:
        agregado: dict[str, dict] = {}
        for ln in self.linhas:
            if ln.tipo not in _TIPOS_ENTRADA or not ln.ativo_nome:
                continue
            chave = _norm(ln.ativo_nome).upper()
            if ln.codigo_ativo:
                chave = ln.codigo_ativo.upper()
            atual = agregado.get(chave)
            if atual is None:
                agregado[chave] = {
                    "nome": ln.ativo_nome,
                    "data": ln.data_mov,
                    "ocorrencias": 1,
                    "codigo": ln.codigo_ativo,
                }
            else:
                atual["ocorrencias"] += 1
                if ln.data_mov < atual["data"]:
                    atual["data"] = ln.data_mov
                if ln.codigo_ativo and not atual.get("codigo"):
                    atual["codigo"] = ln.codigo_ativo
        resultado = [
            AplicacaoExtrato(
                v["nome"], v["data"], v["ocorrencias"], codigo=v.get("codigo")
            )
            for v in agregado.values()
        ]
        resultado.sort(key=lambda a: a.data)
        return resultado

    @property
    def primeira_aplicacao_legacy(self) -> list[AplicacaoDetectada]:
        """Compatível com casar_com_ativos do módulo de movimentações."""
        return [
            AplicacaoDetectada(a.nome, a.data, a.ocorrencias)
            for a in self.primeira_aplicacao_por_ativo
        ]

    @property
    def proventos_propostos(self) -> list[ProventoProposto]:
        out: list[ProventoProposto] = []
        for ln in self.linhas:
            if ln.tipo != "juros" or ln.valor <= 0:
                continue
            nome = ln.ativo_nome or ln.codigo_ativo or ln.lancamento
            out.append(
                ProventoProposto(
                    nome=nome,
                    codigo=ln.codigo_ativo,
                    data=ln.data_mov,
                    valor=ln.valor,
                    lancamento=ln.lancamento,
                )
            )
        return out


def eh_extrato_conta(fonte: Fonte, nome_arquivo: Optional[str] = None) -> bool:
    """True se o arquivo parece ser o Extrato da conta (não Posição Detalhada)."""
    try:
        df = _ler_planilha(fonte, nome_arquivo)
    except Exception:
        return False
    texto = " ".join(
        str(v) for v in df.values.flatten() if v is not None and str(v).strip()
    ).upper()
    if "EXTRATO DA CONTA" not in texto:
        return False
    if "SUA CARTEIRA" in texto or "POSIÇÃO" in texto.replace("POSICAO", "POSIÇÃO"):
        return False
    return True


def _data_mov_iso(x: object) -> Optional[str]:
    """Converte célula de data (str dd/mm/aaaa, Timestamp ou datetime)."""
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    if isinstance(x, datetime):
        return x.date().isoformat()
    if isinstance(x, date):
        return x.isoformat()
    if isinstance(x, pd.Timestamp):
        return x.date().isoformat()
    iso = _data_iso(x)
    if iso:
        return iso
    ts = pd.to_datetime(x, errors="coerce")
    if pd.notna(ts):
        return ts.date().isoformat()
    return None


def _valor_linha(x: object) -> Optional[float]:
    """Valor monetário: aceita número nativo do Excel ou texto R$ formatado."""
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return float(x)
    return _valor(x)


def _classificar_lancamento(lanc: str) -> tuple[str, Optional[str], Optional[str]]:
    """Retorna (tipo, ativo_nome, codigo_ativo)."""
    s = lanc.strip()
    sup = s.upper()

    if "TED APLICAÇÃO FUNDOS" in sup or "TED APLICACAO FUNDOS" in sup:
        m = _RE_FUNDOS.search(s)
        nome = m.group(1).strip() if m else None
        return "aplic_fundo", nome, None

    if sup.startswith("COMPRA "):
        resto = s[7:].strip()
        cod = _RE_CODIGO_COE.search(resto)
        return "compra", resto, cod.group(1).upper() if cod else None

    if sup.startswith("RESGATE "):
        resto = s[8:].strip()
        cod = _RE_CODIGO_COE.search(resto)
        return "resgate", resto, cod.group(1).upper() if cod else None

    if sup.startswith("PGTO JUROS") or "PGTO JUROS" in sup:
        m = _RE_JUROS.search(s)
        if m:
            cod = m.group(1).upper()
            desc = m.group(2).strip()
            return "juros", desc, cod
        cod = _RE_CODIGO_COE.search(s)
        return "juros", s, cod.group(1).upper() if cod else None

    if sup.startswith("IR -") or sup.startswith("IR-"):
        return "ir_juros", None, None

    if "TED TER" in sup and "RESGATE" not in sup:
        return "ted_caixa", None, None

    return "outro", None, None


def _localizar_cabecalho(df: pd.DataFrame) -> int:
    for i in range(min(len(df), 30)):
        row = [_norm(c).lower() for c in df.iloc[i].tolist()]
        if "movimentacao" in row and "lancamento" in row:
            return i
    raise ErroImportacaoXP(
        "Cabeçalho do extrato da conta não encontrado "
        "(esperado: Movimentação, Liquidação, Lançamento)."
    )


def _extrair_metadados(df: pd.DataFrame) -> MetadadosExtratoConta:
    meta = MetadadosExtratoConta()
    for i in range(min(len(df), 15)):
        for val in df.iloc[i].tolist():
            if val is None or (isinstance(val, float) and pd.isna(val)):
                continue
            s = str(val).strip()
            sn = _norm(s)
            if "data da consulta" in sn.lower():
                parte = s.split(":", 1)[-1].strip()
                meta.data_consulta = _data_iso(parte.split()[0] if parte else None)
            m_conta = _RE_CONTA.search(s)
            if m_conta:
                meta.conta = m_conta.group(1)
            m_per = _RE_PERIODO.search(s)
            if m_per:
                meta.periodo_de = _data_iso(m_per.group(1))
                meta.periodo_ate = _data_iso(m_per.group(2))
        # Titular costuma estar na coluna 1, linha 4
        tit = df.iloc[i, 1] if df.shape[1] > 1 else None
        if tit is not None and not pd.isna(tit):
            ts = str(tit).strip()
            if (
                ts
                and "conta xp" not in ts.lower()
                and "proje" not in ts.lower()
                and "saldo" not in ts.lower()
                and "resgate" not in ts.lower()
                and "garantia" not in ts.lower()
                and "termo" not in ts.lower()
                and len(ts) > 5
                and any(ch.isalpha() for ch in ts)
                and meta.titular is None
            ):
                meta.titular = ts
    return meta


def _indice_colunas(cabecalho: list) -> dict[str, int]:
    norm = [_norm(c).lower() for c in cabecalho]
    idx: dict[str, int] = {}
    for i, c in enumerate(norm):
        if "moviment" in c:
            idx["mov"] = i
        elif "liquida" in c:
            idx["liq"] = i
        elif "lan" in c and "amento" in c:
            idx["lanc"] = i
        elif "valor" in c:
            idx["valor"] = i
        elif "saldo" in c:
            idx["saldo"] = i
    if "mov" not in idx or "lanc" not in idx or "valor" not in idx:
        raise ErroImportacaoXP("Colunas obrigatórias ausentes no extrato da conta.")
    return idx


def importar(fonte: Fonte, nome_arquivo: Optional[str] = None) -> ExtratoContaResult:
    """Lê o XLSX do Extrato da conta e retorna metadados + linhas classificadas."""
    df = _ler_planilha(fonte, nome_arquivo)
    meta = _extrair_metadados(df)
    linha_cab = _localizar_cabecalho(df)
    idx = _indice_colunas(df.iloc[linha_cab].tolist())

    linhas: list[LinhaExtratoConta] = []
    for _, row in df.iloc[linha_cab + 1 :].iterrows():
        lanc_raw = row.iloc[idx["lanc"]] if idx["lanc"] < len(row) else None
        if lanc_raw is None or (isinstance(lanc_raw, float) and pd.isna(lanc_raw)):
            continue
        lanc = str(lanc_raw).strip()
        if not lanc or lanc.lower() == "lançamento":
            continue

        data_mov = _data_mov_iso(row.iloc[idx["mov"]] if idx["mov"] < len(row) else None)
        if not data_mov:
            continue

        data_liq = None
        if "liq" in idx and idx["liq"] < len(row):
            data_liq = _data_mov_iso(row.iloc[idx["liq"]])

        valor = _valor_linha(row.iloc[idx["valor"]] if idx["valor"] < len(row) else None)
        if valor is None:
            continue

        saldo = None
        if "saldo" in idx and idx["saldo"] < len(row):
            saldo = _valor_linha(row.iloc[idx["saldo"]])

        tipo, ativo_nome, codigo = _classificar_lancamento(lanc)
        linhas.append(
            LinhaExtratoConta(
                data_mov=data_mov,
                data_liq=data_liq,
                lancamento=lanc,
                valor=valor,
                saldo=saldo,
                tipo=tipo,
                ativo_nome=ativo_nome,
                codigo_ativo=codigo,
            )
        )

    if not linhas:
        raise ErroImportacaoXP("Nenhuma movimentação reconhecida no extrato da conta.")

    datas = sorted(ln.data_mov for ln in linhas)
    meta.periodo_real_de = datas[0]
    meta.periodo_real_ate = datas[-1]
    return ExtratoContaResult(metadados=meta, linhas=linhas)


def casar_aplicacoes_com_ativos(
    aplicacoes: list[AplicacaoExtrato],
    ativos: list,
    limiar: float = 0.55,
) -> list[dict]:
    """Casa aplicações do extrato da conta com ativos (nome + código COE)."""
    propostas: list[dict] = []
    for a in ativos:
        alvo = _norm(a["nome"]).upper()
        melhor = None
        melhor_score = 0.0
        for ap in aplicacoes:
            score_nome = SequenceMatcher(None, alvo, _norm(ap.nome).upper()).ratio()
            score_cod = 0.0
            if ap.codigo and ap.codigo.upper() in alvo:
                score_cod = 1.0
            score = max(score_nome, score_cod)
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


def casar_proventos_com_ativos(
    proventos: list[ProventoProposto],
    ativos: list,
    limiar: float = 0.55,
) -> list[dict]:
    """Casa proventos do extrato com ativos cadastrados."""
    propostas: list[dict] = []
    for pv in proventos:
        alvo_nome = _norm(pv.nome).upper()
        alvo_cod = (pv.codigo or "").upper()
        melhor_id = None
        melhor_nome = None
        melhor_score = 0.0
        for a in ativos:
            nome_ativo = _norm(a["nome"]).upper()
            score_nome = SequenceMatcher(None, alvo_nome, nome_ativo).ratio()
            score_cod = 1.0 if alvo_cod and alvo_cod in nome_ativo else 0.0
            score = max(score_nome, score_cod)
            if score > melhor_score:
                melhor_score = score
                melhor_id = int(a["id"])
                melhor_nome = a["nome"]
        if melhor_id is not None and melhor_score >= limiar:
            propostas.append(
                {
                    "ativo_id": melhor_id,
                    "ativo": melhor_nome,
                    "data": pv.data,
                    "valor": pv.valor,
                    "tipo": "juros",
                    "origem": pv.lancamento,
                    "score": round(melhor_score, 2),
                }
            )
    return propostas


def serie_saldo(resultado: ExtratoContaResult) -> pd.DataFrame:
    """DataFrame com colunas data, saldo (último saldo por dia)."""
    rows = [
        {"data": ln.data_mov, "saldo": ln.saldo}
        for ln in resultado.linhas
        if ln.saldo is not None
    ]
    if not rows:
        return pd.DataFrame(columns=["data", "saldo"])
    df = pd.DataFrame(rows)
    df["data"] = pd.to_datetime(df["data"])
    return df.groupby("data", as_index=False)["saldo"].last().sort_values("data")


def fluxo_mensal(resultado: ExtratoContaResult) -> pd.DataFrame:
    """Agrega entradas, saídas e proventos por mês."""
    buckets: dict[str, dict[str, float]] = {}
    for ln in resultado.linhas:
        mes = ln.data_mov[:7]
        if mes not in buckets:
            buckets[mes] = {"entradas": 0.0, "saidas": 0.0, "proventos": 0.0}
        v = abs(ln.valor)
        if ln.tipo in _TIPOS_ENTRADA:
            buckets[mes]["entradas"] += v
        elif ln.tipo in _TIPOS_SAIDA:
            buckets[mes]["saidas"] += v
        elif ln.tipo in _TIPOS_PROVENTO and ln.valor > 0:
            buckets[mes]["proventos"] += v
    if not buckets:
        return pd.DataFrame(columns=["mes", "entradas", "saidas", "proventos"])
    df = pd.DataFrame(
        [{"mes": k, **v} for k, v in sorted(buckets.items())]
    )
    df["mes"] = pd.to_datetime(df["mes"] + "-01")
    return df


def titular_sugerido(nome_extrato: Optional[str], titulares: list) -> Optional[str]:
    """Sugere titular cadastrado a partir do nome no extrato."""
    if not nome_extrato:
        return None
    alvo = _norm(nome_extrato).upper()
    melhor = None
    melhor_score = 0.0
    for t in titulares:
        nome = _norm(t["nome"]).upper()
        score = SequenceMatcher(None, alvo, nome).ratio()
        if nome in alvo or alvo.split()[0] in nome:
            score = max(score, 0.85)
        if score > melhor_score:
            melhor_score = score
            melhor = t["nome"]
    return melhor if melhor_score >= 0.5 else None
