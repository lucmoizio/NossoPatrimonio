"""Investimentos & Patrimônio Lutcho e Lidy — pacote de domínio.

Módulos:
    database   — persistência SQLite + migrações leves
    mercado    — indicadores oficiais do Banco Central (SGS)
    cvm        — atualização automática de fundos via Informe Diário
    analise    — motor de análise por ativo + consolidação + alertas
    projecao   — juros compostos, cenários, anos até a meta
    relatorios — evolução patrimonial, proventos, relatório de IR
    consultor  — Consultor IA (Anthropic API + web search)
    simulador  — paper trading B3 com travas
"""

__version__ = "1.3.0"
