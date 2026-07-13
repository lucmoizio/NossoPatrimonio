"""Motor de análise: rentabilidade por ativo, alertas e consolidação.

Todas as comparações são contra o CDI oficial (custo de oportunidade). O IR
é **estimado** pela tabela regressiva da Lei 11.033/2004 e é sempre rotulado
como estimativa na UI — o Informe de Rendimentos oficial prevalece.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

from . import database, mercado

# --------------------------------------------------------------------------- #
# Parâmetros de negócio
# --------------------------------------------------------------------------- #
# Tabela regressiva de IR sobre rendimentos (Lei 11.033/2004).
FAIXAS_IR = (
    (180, 0.225),   # até 180 dias
    (360, 0.20),    # 181 a 360
    (720, 0.175),   # 361 a 720
    (float("inf"), 0.15),  # acima de 720
)

# Limiares de alertas.
LIMIAR_CDI_REVISAO = 0.70   # %CDI < 70% → revisar
LIMIAR_CDI_ATENCAO = 0.90   # %CDI < 90% → atenção
LIMIAR_TAXA_ADM = 1.5       # taxa adm >= 1,5% a.a. → alerta
DIAS_SNAPSHOT_VELHO = 45     # snapshot mais velho que isso → alerta


def aliquota_ir(dias: int) -> float:
    """Alíquota de IR (fração) conforme prazo em dias — Lei 11.033/2004."""
    for limite, aliq in FAIXAS_IR:
        if dias <= limite:
            return aliq
    return 0.15


@dataclass
class Alerta:
    """Alerta gerado para um ativo (nível: 'revisao' | 'atencao' | 'info').

    `mensagem` é o resumo curto; `detalhe` explica o porquê com números; `acao`
    indica a direção sugerida considerando o montante investido.
    """

    nivel: str
    mensagem: str
    detalhe: str = ""
    acao: str = ""


def _brl(valor: float) -> str:
    """Formata BRL para uso nas mensagens (evita depender da camada de UI)."""
    s = f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


@dataclass
class AnaliseAtivo:
    """Resultado da análise de um ativo individual."""

    ativo_id: int
    nome: str
    titular: str
    categoria: str
    valor_aplicado: float
    valor_atual: float                       # saldo da posição (snapshot)
    data_snapshot: Optional[str]
    rent_bruta: float                        # retorno total do período
    cdi_periodo: Optional[float]
    ipca_periodo: Optional[float]
    pct_cdi: Optional[float]
    rent_real: Optional[float]
    ir_estimado: float
    aliquota_ir_pct: float
    ganho_bruto: float
    rent_fonte: str = "posicao"              # 'posicao' | 'extrato'
    valor_economico: Optional[float] = None  # aplicado × (1+rent) quando extrato
    alertas: list[Alerta] = field(default_factory=list)


def _rent_bruta_extrato(ativo: Any) -> Optional[float]:
    """Rentabilidade total gravada do extrato XP (fração), ou None."""
    if "rent_bruta_extrato" not in ativo.keys():
        return None
    v = ativo["rent_bruta_extrato"]
    if v is None:
        return None
    try:
        f = float(v)
        return f if f > -1.0 else None
    except (TypeError, ValueError):
        return None


def _dias_entre(inicio: str, fim: str) -> int:
    d0 = datetime.fromisoformat(inicio).date()
    d1 = datetime.fromisoformat(fim).date()
    return max(0, (d1 - d0).days)


def analisar_ativo(ativo: Any, hoje: Optional[str] = None) -> AnaliseAtivo:
    """Analisa um ativo (Row de `ativos` + join titular) contra CDI/IPCA.

    Usa o último snapshot como valor da posição. Quando `rent_bruta_extrato`
    estiver gravado (campo Rentabilidade do extrato XP), usa esse retorno total
    — que inclui cupons pagos fora da posição — para rent_bruta, %CDI e ganho.
    """
    hoje = hoje or date.today().isoformat()
    ativo_id = int(ativo["id"])
    valor_aplicado = float(ativo["valor_aplicado"] or 0.0)

    snap = database.ultimo_snapshot(ativo_id)
    if snap is not None:
        valor_posicao = float(snap["valor"])
        data_snapshot = snap["data"]
    else:
        valor_posicao = valor_aplicado
        data_snapshot = None

    rent_extrato = _rent_bruta_extrato(ativo)
    if rent_extrato is not None and valor_aplicado > 0:
        rent_bruta = rent_extrato
        valor_economico = round(valor_aplicado * (1.0 + rent_bruta), 2)
        ganho_bruto = round(valor_economico - valor_aplicado, 2)
        rent_fonte = "extrato"
    else:
        rent_bruta = (valor_posicao / valor_aplicado - 1.0) if valor_aplicado > 0 else 0.0
        ganho_bruto = valor_posicao - valor_aplicado
        valor_economico = None
        rent_fonte = "posicao"

    valor_atual = valor_posicao

    # Período do ativo (da 1ª aplicação até hoje) para benchmarks.
    data_aplic = ativo["data_aplicacao"]
    cdi_periodo: Optional[float] = None
    ipca_periodo: Optional[float] = None
    if data_aplic:
        try:
            cdi_periodo = mercado.cdi_acumulado(data_aplic, hoje)
        except mercado.ErroDadosMercado:
            cdi_periodo = None
        try:
            ipca_periodo = mercado.ipca_acumulado(data_aplic, hoje)
        except mercado.ErroDadosMercado:
            ipca_periodo = None

    pct_cdi = (rent_bruta / cdi_periodo) if cdi_periodo and cdi_periodo > 0 else None
    rent_real = (
        (1.0 + rent_bruta) / (1.0 + ipca_periodo) - 1.0
        if ipca_periodo is not None
        else None
    )

    # IR estimado (rotulado como estimativa na UI).
    isento = bool(ativo["isento_ir"])
    if isento or ganho_bruto <= 0 or not data_aplic:
        aliq = 0.0
        ir_estimado = 0.0
    else:
        dias = _dias_entre(data_aplic, data_snapshot or hoje)
        aliq = aliquota_ir(dias)
        ir_estimado = round(ganho_bruto * aliq, 2)

    analise = AnaliseAtivo(
        ativo_id=ativo_id,
        nome=ativo["nome"],
        titular=ativo["titular_nome"] if "titular_nome" in ativo.keys() else "",
        categoria=ativo["categoria"],
        valor_aplicado=valor_aplicado,
        valor_atual=valor_atual,
        data_snapshot=data_snapshot,
        rent_bruta=rent_bruta,
        cdi_periodo=cdi_periodo,
        ipca_periodo=ipca_periodo,
        pct_cdi=pct_cdi,
        rent_real=rent_real,
        ir_estimado=ir_estimado,
        aliquota_ir_pct=round(aliq * 100, 1),
        ganho_bruto=ganho_bruto,
        rent_fonte=rent_fonte,
        valor_economico=valor_economico,
    )
    analise.alertas = _gerar_alertas(ativo, analise, hoje)
    return analise


def _gerar_alertas(ativo: Any, a: AnaliseAtivo, hoje: str) -> list[Alerta]:
    """Aplica as regras de alerta descritas em §4.4, com detalhe e ação."""
    alertas: list[Alerta] = []
    montante = _brl(a.valor_aplicado)

    if a.pct_cdi is not None:
        fonte_rent = (
            " (rentabilidade total do extrato XP, inclui cupons fora da posição)"
            if a.rent_fonte == "extrato"
            else ""
        )
        if a.pct_cdi < LIMIAR_CDI_REVISAO:
            alertas.append(
                Alerta(
                    "revisao",
                    f"Rende {a.pct_cdi:.0%} do CDI (< 70%) — revisar aplicação.",
                    detalhe=(
                        f"Desde a aplicação o ativo rendeu {a.rent_bruta:.1%} (bruto{fonte_rent}), "
                        f"enquanto o CDI do mesmo período acumulou {a.cdi_periodo:.1%}. "
                        f"Isso equivale a apenas {a.pct_cdi:.0%} do CDI — bem abaixo do "
                        "custo de oportunidade de um pós-fixado simples."
                    ),
                    acao=(
                        f"Prioridade de revisão. Há {montante} aplicados aqui: avalie "
                        "realocar para um pós-fixado que renda perto de 100% do CDI, "
                        "considerando o IR pelo prazo e a liquidez antes de resgatar."
                    ),
                )
            )
        elif a.pct_cdi < LIMIAR_CDI_ATENCAO:
            alertas.append(
                Alerta(
                    "atencao",
                    f"Rende {a.pct_cdi:.0%} do CDI (< 90%) — atenção.",
                    detalhe=(
                        f"Rendimento bruto de {a.rent_bruta:.1%} contra {a.cdi_periodo:.1%} "
                        f"de CDI no período ({a.pct_cdi:.0%} do CDI). Ainda aceitável, "
                        "mas abaixo do ideal."
                    ),
                    acao=(
                        f"Acompanhar. Sobre {montante}: se seguir abaixo de 90% do CDI "
                        "nos próximos meses, considere realocação."
                    ),
                )
            )

    if a.rent_real is not None and a.rent_real < 0:
        alertas.append(
            Alerta(
                "atencao",
                "Rentabilidade real negativa (perde para a inflação).",
                detalhe=(
                    f"Descontada a inflação do período ({a.ipca_periodo:.1%} de IPCA), a "
                    f"rentabilidade real é {a.rent_real:.1%}. O poder de compra de "
                    f"{montante} está sendo corroído."
                ),
                acao=(
                    "Reavaliar: o ativo não está preservando valor real. Compare com "
                    "alternativas indexadas à inflação (ex.: Tesouro IPCA+) ou ao CDI."
                ),
            )
        )

    if str(a.categoria).upper() == "COE":
        det_coe = (
            "COEs têm resgate apenas no vencimento, payoff condicional e custos "
            "embutidos pouco transparentes."
        )
        if a.rent_fonte == "extrato":
            det_coe += (
                f" A rentabilidade usada ({a.rent_bruta:.1%}) veio do extrato XP e "
                "inclui cupons — compare também com a taxa prefixada contratada, "
                "não só com o CDI."
            )
        else:
            det_coe += (
                " A rentabilidade atual usa só o saldo da posição; se o COE paga "
                "cupons na conta, reimporte o extrato XP para incluir a rentabilidade total."
            )
        alertas.append(
            Alerta(
                "atencao",
                "COE: liquidez e cenários limitados — revisar condições.",
                detalhe=det_coe,
                acao=(
                    f"Revisar o vencimento e as condições dos {montante}. Evitar novos "
                    "aportes; ao vencer, redirecionar para produtos mais líquidos e claros."
                ),
            )
        )

    taxa_adm = ativo["taxa_adm_aa"]
    if taxa_adm is not None and float(taxa_adm) >= LIMIAR_TAXA_ADM:
        alertas.append(
            Alerta(
                "atencao",
                f"Taxa de administração alta ({float(taxa_adm):.2f}% a.a.).",
                detalhe=(
                    f"A taxa de {float(taxa_adm):.2f}% a.a. incide sobre todo o montante "
                    "todo ano e corrói o retorno líquido, sobretudo em renda fixa."
                ),
                acao=(
                    f"Sobre {montante}: comparar com fundos equivalentes de taxa menor. "
                    "A taxa só se justifica com desempenho consistente acima dos pares."
                ),
            )
        )

    if a.data_snapshot:
        dias = _dias_entre(a.data_snapshot, hoje)
        if dias > DIAS_SNAPSHOT_VELHO:
            alertas.append(
                Alerta(
                    "info",
                    f"Último valor tem {dias} dias — atualize o snapshot.",
                    detalhe=(
                        f"O último valor registrado é de {dias} dias atrás; a análise de "
                        "rentabilidade pode estar defasada."
                    ),
                    acao="Atualize o valor na aba 📝 Atualizar valores (manual ou via CVM).",
                )
            )
    else:
        alertas.append(
            Alerta(
                "info",
                "Sem snapshot de valor registrado.",
                detalhe="Sem um valor atual, a rentabilidade é assumida como zero.",
                acao="Registre um snapshot de valor na aba 📝 Atualizar valores.",
            )
        )

    return alertas


@dataclass
class Consolidacao:
    """Visão consolidada da carteira (opcionalmente filtrada por titular)."""

    total_atual: float
    total_aplicado: float
    ganho: float
    rent_media_ponderada: float
    alocacao_categoria: dict[str, float]
    analises: list[AnaliseAtivo]
    alertas: list[tuple[str, Alerta]]  # (nome do ativo, alerta)


def consolidar(titular_id: Optional[int] = None, hoje: Optional[str] = None) -> Consolidacao:
    """Analisa todos os ativos (do titular, se informado) e consolida totais."""
    hoje = hoje or date.today().isoformat()
    ativos = database.listar_ativos(titular_id=titular_id)
    analises = [analisar_ativo(a, hoje) for a in ativos]

    total_atual = sum(a.valor_atual for a in analises)
    total_aplicado = sum(a.valor_aplicado for a in analises)
    ganho = total_atual - total_aplicado
    rent_media = (ganho / total_aplicado) if total_aplicado > 0 else 0.0

    alocacao: dict[str, float] = {}
    for a in analises:
        alocacao[a.categoria] = alocacao.get(a.categoria, 0.0) + a.valor_atual

    alertas: list[tuple[str, Alerta]] = []
    for a in analises:
        for alerta in a.alertas:
            alertas.append((a.nome, alerta))

    # Ordena: revisao > atencao > info.
    ordem = {"revisao": 0, "atencao": 1, "info": 2}
    alertas.sort(key=lambda par: ordem.get(par[1].nivel, 9))

    return Consolidacao(
        total_atual=round(total_atual, 2),
        total_aplicado=round(total_aplicado, 2),
        ganho=round(ganho, 2),
        rent_media_ponderada=rent_media,
        alocacao_categoria=alocacao,
        analises=analises,
        alertas=alertas,
    )
