"""Motor de comparacao e recomendacao de fundos (fontes oficiais CVM+BCB).

Compara cada fundo da carteira contra a MEDIANA da sua classe CVM em janelas de
6/12/24 meses e sinaliza "baixo desempenho" quando o fundo fica abaixo da
mediana em >= 2 das 3 janelas. Quando sinalizado, sugere fundos da mesma classe
com desempenho consistente acima da mediana.

Principios respeitados:
- Somente dados oficiais (Informe Diario e cadastro da CVM; CDI do BCB).
- Saida estritamente INFORMATIVA — nao e recomendacao de compra/venda.
- Nada inventado: o que nao puder ser calculado vem como None.

Custo: `estatisticas_classe` le alguns Informes Diarios mensais inteiros (arquivos
grandes) para montar o universo por classe; por isso e uma rotina de lote
(chamada pelo job/CLI ou por um botao com spinner), com resultado cacheado em
`pares_classe`/`universo_retornos`. As leituras por fundo usam o cache local.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from . import cvm, database, mercado

JANELAS_PADRAO = (6, 12, 24)
_DIAS_UTEIS_ANO = 252


# --------------------------------------------------------------------------- #
# Datas
# --------------------------------------------------------------------------- #
def _subtrai_meses(d: date, meses: int) -> date:
    """Subtrai `meses` de uma data, ajustando o dia ao fim do mes se preciso."""
    total = (d.year * 12 + (d.month - 1)) - meses
    ano, mes = divmod(total, 12)
    mes += 1
    # ajusta o dia para nao estourar (ex.: 31 -> ultimo dia do mes destino)
    import calendar

    ultimo = calendar.monthrange(ano, mes)[1]
    return date(ano, mes, min(d.day, ultimo))


def _mes(d: date) -> str:
    return f"{d.year:04d}{d.month:02d}"


# --------------------------------------------------------------------------- #
# Estruturas de saida
# --------------------------------------------------------------------------- #
@dataclass
class DesempenhoJanela:
    janela: int
    retorno: Optional[float]
    mediana_classe: Optional[float]
    abaixo: bool


@dataclass
class Alternativa:
    cnpj: str
    denominacao: str
    retornos: dict[int, float]
    janelas_acima: int
    taxa_adm: Optional[float] = None


@dataclass
class AvaliacaoFundo:
    cnpj: str
    nome: str
    classe: Optional[str]
    valor_aplicado: float
    janelas: list[DesempenhoJanela]
    baixo_desempenho: bool
    vol_anual: Optional[float] = None
    sharpe: Optional[float] = None
    alternativas: list[Alternativa] = field(default_factory=list)
    obs: str = ""


# --------------------------------------------------------------------------- #
# Retornos por janela (fundo isolado — usa o cache local)
# --------------------------------------------------------------------------- #
def retornos_fundo(
    cnpj: str, data_ref: str, janelas: tuple[int, ...] = JANELAS_PADRAO
) -> dict[int, Optional[float]]:
    """Retorno por janela do fundo = cota(data_ref) / cota(data_ref - X meses) - 1."""
    ref = datetime.fromisoformat(data_ref).date()
    fim = cvm.cota_na_data(cnpj, data_ref)
    saida: dict[int, Optional[float]] = {j: None for j in janelas}
    if fim is None:
        return saida
    _, cota_fim = fim
    for j in janelas:
        alvo = _subtrai_meses(ref, j).isoformat()
        ini = cvm.cota_na_data(cnpj, alvo)
        if ini is not None and ini[1] > 0:
            saida[j] = cota_fim / ini[1] - 1.0
    return saida


# --------------------------------------------------------------------------- #
# Estatisticas por classe (universo — rotina de lote)
# --------------------------------------------------------------------------- #
def _universo_mes_seguro(alvo: date, limite: date, recuos: int = 3) -> dict[str, float]:
    """universo_cotas_mes com fallback para meses anteriores se o informe faltar."""
    ano, mes = alvo.year, alvo.month
    for _ in range(recuos + 1):
        try:
            mp = cvm.universo_cotas_mes(f"{ano:04d}{mes:02d}", limite.isoformat())
        except cvm.ErroDadosCVM:
            mp = {}
        if mp:
            return mp
        mes -= 1
        if mes <= 0:
            mes += 12
            ano -= 1
    return {}


def estatisticas_classe(
    janelas: tuple[int, ...] = JANELAS_PADRAO,
    data_ref: Optional[str] = None,
    caminho=None,
) -> dict:
    """Monta o universo de retornos por classe e grava medianas em `pares_classe`.

    Retorna um resumo {data_ref, janelas: {janela: n_classes}}. Operacao pesada
    (le Informes mensais inteiros) — chamar em lote, nao no page load.
    """
    ref = datetime.fromisoformat(data_ref).date() if data_ref else date.today()
    fim_map = _universo_mes_seguro(ref, ref)
    if not fim_map:
        return {"data_ref": ref.isoformat(), "janelas": {}, "erro": "Informe indisponivel"}

    classes = {r["cnpj"]: r["classe"] for r in database.todas_classes(caminho=caminho)}
    resumo_janelas: dict[int, int] = {}
    registros_universo: list[dict] = []
    registros_pares: list[dict] = []

    for j in janelas:
        alvo = _subtrai_meses(ref, j)
        ini_map = _universo_mes_seguro(alvo, alvo)
        if not ini_map:
            continue
        por_classe: dict[str, list[float]] = {}
        for cnpj, cota_fim in fim_map.items():
            cota_ini = ini_map.get(cnpj)
            if cota_ini is None or cota_ini <= 0:
                continue
            retorno = cota_fim / cota_ini - 1.0
            registros_universo.append(
                {"cnpj": cnpj, "janela": j, "data_ref": ref.isoformat(), "retorno": retorno}
            )
            classe = classes.get(cnpj)
            if classe:
                por_classe.setdefault(classe, []).append(retorno)

        for classe, valores in por_classe.items():
            if len(valores) < 3:  # mediana pouco informativa
                continue
            mediana = statistics.median(valores)
            q1 = q3 = None
            if len(valores) >= 4:
                quart = statistics.quantiles(valores, n=4)
                q1, q3 = quart[0], quart[2]
            registros_pares.append(
                {
                    "classe": classe, "janela": j, "data_ref": ref.isoformat(),
                    "mediana": mediana, "q1": q1, "q3": q3, "n": len(valores),
                }
            )
        resumo_janelas[j] = len(por_classe)

    if registros_universo:
        database.gravar_universo_retornos(registros_universo, caminho=caminho)
    if registros_pares:
        database.gravar_pares_classe(registros_pares, caminho=caminho)

    return {"data_ref": ref.isoformat(), "janelas": resumo_janelas}


# --------------------------------------------------------------------------- #
# Risco (complementar) — apenas para fundos da carteira
# --------------------------------------------------------------------------- #
def _risco(cnpj: str, cdi_aa: Optional[float], caminho=None) -> tuple[Optional[float], Optional[float]]:
    """Volatilidade anualizada e Sharpe a partir da serie diaria em cache."""
    serie = database.serie_cotas_fundo(cnpj, caminho=caminho)
    if len(serie) < 30:
        return None, None
    cotas = [float(r["cota"]) for r in serie if r["cota"]]
    # ultimos ~252 pregoes
    cotas = cotas[-_DIAS_UTEIS_ANO:]
    retornos = [
        cotas[i] / cotas[i - 1] - 1.0
        for i in range(1, len(cotas))
        if cotas[i - 1] > 0
    ]
    if len(retornos) < 20:
        return None, None
    try:
        vol_diaria = statistics.pstdev(retornos)
    except statistics.StatisticsError:
        return None, None
    vol_anual = vol_diaria * (_DIAS_UTEIS_ANO ** 0.5)
    if vol_anual <= 0:
        return round(vol_anual, 4), None
    ret_anual = (cotas[-1] / cotas[0]) ** (_DIAS_UTEIS_ANO / max(1, len(cotas) - 1)) - 1.0
    if cdi_aa is None:
        return round(vol_anual, 4), None
    sharpe = (ret_anual - cdi_aa / 100.0) / vol_anual
    return round(vol_anual, 4), round(sharpe, 3)


# --------------------------------------------------------------------------- #
# Sugestao de alternativas
# --------------------------------------------------------------------------- #
def sugerir_alternativas(
    classe: str,
    taxa_atual: Optional[float],
    data_ref: str,
    janelas: tuple[int, ...] = JANELAS_PADRAO,
    top: int = 3,
    excluir_cnpj: Optional[str] = None,
    caminho=None,
) -> list[Alternativa]:
    """Fundos da mesma classe acima da mediana em >= 2 janelas, melhores primeiro.

    Filtra por taxa de administracao <= a atual apenas quando a taxa do candidato
    for conhecida (o cadastro da CVM nao traz taxa — ver README).
    """
    linhas = database.retornos_por_classe(classe, data_ref, caminho=caminho)
    if not linhas:
        return []
    medianas = {
        j: (database.obter_pares_classe(classe, j, data_ref, caminho=caminho))
        for j in janelas
    }
    medianas = {j: (m["mediana"] if m else None) for j, m in medianas.items()}

    por_cnpj: dict[str, dict] = {}
    for l in linhas:
        d = por_cnpj.setdefault(
            l["cnpj"],
            {"denominacao": l["denominacao"], "taxa_adm": l["taxa_adm"], "retornos": {}},
        )
        d["retornos"][int(l["janela"])] = float(l["retorno"])

    candidatos: list[Alternativa] = []
    excl = "".join(ch for ch in str(excluir_cnpj or "") if ch.isdigit())
    for cnpj, info in por_cnpj.items():
        if cnpj == excl:
            continue
        acima = sum(
            1
            for j in janelas
            if info["retornos"].get(j) is not None
            and medianas.get(j) is not None
            and info["retornos"][j] > medianas[j]
        )
        if acima < 2:
            continue
        if (
            taxa_atual is not None
            and info["taxa_adm"] is not None
            and float(info["taxa_adm"]) > float(taxa_atual)
        ):
            continue
        candidatos.append(
            Alternativa(
                cnpj=cnpj,
                denominacao=info["denominacao"] or cnpj,
                retornos={j: info["retornos"][j] for j in janelas if j in info["retornos"]},
                janelas_acima=acima,
                taxa_adm=info["taxa_adm"],
            )
        )

    chave_ord = 12 if 12 in janelas else janelas[0]
    candidatos.sort(
        key=lambda a: (a.janelas_acima, a.retornos.get(chave_ord, -9.9)), reverse=True
    )
    return candidatos[:top]


# --------------------------------------------------------------------------- #
# Avaliacao da carteira
# --------------------------------------------------------------------------- #
def avaliar_carteira(
    titular_id: Optional[int] = None,
    janelas: tuple[int, ...] = JANELAS_PADRAO,
    caminho=None,
) -> list[AvaliacaoFundo]:
    """Avalia cada fundo da carteira (com CNPJ) contra a mediana da sua classe."""
    data_ref = database.data_ref_universo(caminho=caminho) or date.today().isoformat()
    try:
        cdi_aa = mercado.indicadores_atuais().cdi_aa
    except Exception:
        cdi_aa = None

    avaliacoes: list[AvaliacaoFundo] = []
    for a in database.listar_ativos(titular_id=titular_id, apenas_ativos=True, caminho=caminho):
        cnpj = "".join(ch for ch in str(a["cnpj"] or "") if ch.isdigit())
        if len(cnpj) != 14:
            continue
        cad = database.obter_fundo_cadastro(cnpj, caminho=caminho)
        classe = cad["classe"] if cad else None
        ret = retornos_fundo(cnpj, data_ref, janelas)

        infos: list[DesempenhoJanela] = []
        abaixo_count = 0
        for j in janelas:
            par = database.obter_pares_classe(classe, j, data_ref, caminho=caminho) if classe else None
            mediana = par["mediana"] if par else None
            r = ret[j]
            abaixo = r is not None and mediana is not None and r < mediana
            if abaixo:
                abaixo_count += 1
            infos.append(DesempenhoJanela(j, r, mediana, abaixo))

        baixo = abaixo_count >= 2
        vol, sharpe = _risco(cnpj, cdi_aa, caminho=caminho)
        alternativas: list[Alternativa] = []
        obs = ""
        if classe is None:
            obs = "Classe CVM desconhecida (rode a sincronizacao do cadastro)."
        elif not any(i.mediana_classe is not None for i in infos):
            obs = "Sem estatisticas de classe ainda (rode 'Atualizar universo')."
        if baixo and classe:
            alternativas = sugerir_alternativas(
                classe, a["taxa_adm_aa"], data_ref, janelas, excluir_cnpj=cnpj, caminho=caminho
            )

        avaliacoes.append(
            AvaliacaoFundo(
                cnpj=cnpj,
                nome=a["nome"],
                classe=classe,
                valor_aplicado=float(a["valor_aplicado"] or 0.0),
                janelas=infos,
                baixo_desempenho=baixo,
                vol_anual=vol,
                sharpe=sharpe,
                alternativas=alternativas,
                obs=obs,
            )
        )
    # prioriza baixo desempenho e maior montante
    avaliacoes.sort(key=lambda x: (not x.baixo_desempenho, -x.valor_aplicado))
    return avaliacoes


if __name__ == "__main__":  # smoke manual (requer rede + cadastro sincronizado)
    database.inicializar()
    print(estatisticas_classe())
