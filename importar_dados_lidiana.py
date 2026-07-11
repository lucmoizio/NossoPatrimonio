"""Seed da carteira XP da Lidiana (conta 128199, ref. 03/07/2026).

TEMPLATE — este arquivo é a estrutura de importação. Os 18 ativos precisam ser
preenchidos com os dados reais do relatório de performance/extrato da XP. Os
campos marcados como PREENCHER (valor_aplicado, data_aplicacao, taxa_adm_aa)
não constam do extrato de posição e devem ser completados pelo usuário.

Como usar:
    1. Preencha a lista ATIVOS abaixo com os dados de cada linha do extrato.
    2. Rode:  python importar_dados_lidiana.py
    3. O script cadastra os ativos, registra o snapshot da data de referência
       e valida o CNPJ pela cota implícita quando quantidade/cota forem informadas.

Validação de CNPJ (técnica descrita em §8/R3): a cota implícita do extrato
(valor ÷ quantidade) deve bater com a cota oficial da CVM na data de referência
(tolerância padrão 0,5%). Fundos que passam são marcados como validados.

NENHUM dado é inventado: se a lista abaixo estiver com placeholders, o script
avisa e não grava nada.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from patrimonio import cvm, database

TITULAR = "Lidiana"
CONTA = "128199"
DATA_REFERENCIA = "2026-07-03"
TOLERANCIA_COTA = 0.005  # 0,5%

# Marcadores de pendência (conforme §8).
PREENCHER = None


@dataclass
class AtivoSeed:
    """Uma linha da carteira a ser importada.

    Campos do extrato de posição:
        nome, categoria, cnpj (fundos), valor_bruto (na DATA_REFERENCIA),
        quantidade_cotas (opcional — habilita validação de CNPJ por cota).
    Campos que NÃO constam do extrato de posição (PREENCHER):
        valor_aplicado, data_aplicacao, taxa_adm_aa.
    """

    nome: str
    categoria: str
    valor_bruto: float
    cnpj: Optional[str] = None
    quantidade_cotas: Optional[float] = None
    liquidez: Optional[str] = None
    isento_ir: bool = False
    come_cotas: bool = False
    # Pendências a completar manualmente:
    valor_aplicado: Optional[float] = PREENCHER
    data_aplicacao: Optional[str] = PREENCHER
    taxa_adm_aa: Optional[float] = PREENCHER
    observacoes: str = ""


# --------------------------------------------------------------------------- #
# LISTA DOS 18 ATIVOS — PREENCHER com os dados reais do extrato XP.
# Total esperado (conferência): R$ 558.293,71.
# Mantido vazio de propósito: o sistema nunca inventa dados.
# Exemplo de linha (remova o comentário e ajuste aos valores reais):
#   AtivoSeed(
#       nome="XP Crédito Estruturado 360 FIC FIM CP",
#       categoria="Crédito Privado",
#       cnpj="00.000.000/0001-00",
#       valor_bruto=00000.00,
#       quantidade_cotas=0000.000000,
#       liquidez="D+30",
#       observacoes="conta XP 128199",
#   ),
# --------------------------------------------------------------------------- #
ATIVOS: list[AtivoSeed] = [
    # PREENCHER: adicione aqui as 18 linhas do extrato.
]

TOTAL_ESPERADO = 558_293.71


def _validar_cnpj_por_cota(a: AtivoSeed) -> Optional[str]:
    """Confere a cota implícita (valor/qtd) contra a cota oficial da CVM.

    Retorna uma string de status legível, ou None se a validação não se aplica
    (sem CNPJ ou sem quantidade).
    """
    if not a.cnpj or not a.quantidade_cotas:
        return None
    try:
        ref = cvm.cota_na_data(a.cnpj, DATA_REFERENCIA)
    except cvm.ErroDadosCVM as exc:
        return f"não validado (CVM indisponível: {exc})"
    if ref is None:
        return "não validado (cota não encontrada na CVM — pode ser FIDC)"
    _data_cota, cota_oficial = ref
    cota_implicita = a.valor_bruto / a.quantidade_cotas
    desvio = abs(cota_implicita / cota_oficial - 1.0)
    if desvio <= TOLERANCIA_COTA:
        return f"CNPJ validado (desvio {desvio:.3%})"
    return f"DIVERGÊNCIA de cota ({desvio:.3%} > {TOLERANCIA_COTA:.1%}) — conferir CNPJ"


def importar() -> None:
    """Executa a importação da carteira (idempotência: cria novos registros)."""
    database.inicializar()

    if not ATIVOS:
        print(
            "Nenhum ativo preenchido. Edite a lista ATIVOS em "
            "importar_dados_lidiana.py com os dados reais do extrato XP "
            f"(conta {CONTA}, ref. {DATA_REFERENCIA}) e rode novamente.\n"
            "O sistema não inventa dados — nada foi gravado."
        )
        return

    titular_id = database.id_titular(TITULAR)
    if titular_id is None:
        raise RuntimeError(f"Titular '{TITULAR}' não encontrado.")

    total = 0.0
    print(f"Importando carteira de {TITULAR} (conta {CONTA}, ref. {DATA_REFERENCIA})\n")
    for a in ATIVOS:
        pendencias = [
            campo
            for campo, valor in (
                ("valor_aplicado", a.valor_aplicado),
                ("data_aplicacao", a.data_aplicacao),
                ("taxa_adm_aa", a.taxa_adm_aa),
            )
            if valor is None
        ]

        ativo_id = database.inserir_ativo(
            titular_id=titular_id,
            nome=a.nome,
            categoria=a.categoria,
            liquidez=a.liquidez,
            taxa_adm_aa=a.taxa_adm_aa,
            isento_ir=a.isento_ir,
            come_cotas=a.come_cotas,
            data_aplicacao=a.data_aplicacao,
            valor_aplicado=a.valor_aplicado or 0.0,
            observacoes=(a.observacoes + (f" | PREENCHER: {', '.join(pendencias)}" if pendencias else "")).strip(" |"),
            cnpj=a.cnpj,
        )
        database.registrar_snapshot(ativo_id, DATA_REFERENCIA, a.valor_bruto)
        total += a.valor_bruto

        status_cnpj = _validar_cnpj_por_cota(a)
        linha = f"  • {a.nome}: {a.valor_bruto:,.2f}"
        if status_cnpj:
            linha += f"  [{status_cnpj}]"
        if pendencias:
            linha += f"  (PREENCHER: {', '.join(pendencias)})"
        print(linha)

    print(f"\nTotal importado: R$ {total:,.2f}")
    diff = abs(total - TOTAL_ESPERADO)
    if diff > 0.01:
        print(f"ATENÇÃO: diverge do total esperado (R$ {TOTAL_ESPERADO:,.2f}) em R$ {diff:,.2f}.")
    else:
        print(f"Confere com o total esperado (R$ {TOTAL_ESPERADO:,.2f}).")


if __name__ == "__main__":
    importar()
