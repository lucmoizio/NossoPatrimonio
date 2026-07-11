"""Constantes de domínio compartilhadas (listas fechadas).

Ficam aqui (e não em app.py) para poderem ser reutilizadas por módulos do
pacote — como o importador de extratos — sem criar import circular com a UI.
"""

from __future__ import annotations

# Categorias de ativo (lista fechada usada na UI e no importador).
CATEGORIAS = [
    "Renda Fixa - Pós-fixado",
    "Renda Fixa - Prefixado",
    "Renda Fixa - Inflação",
    "Crédito Privado",
    "Fundo Multimercado",
    "Fundo de Ações",
    "Fundo Imobiliário",
    "FIDC",
    "COE",
    "Ações",
    "Tesouro Direto",
    "LCI/LCA",
    "CDB",
    "Poupança",
    "Outro",
]

# Opções de liquidez.
LIQUIDEZ = ["D+0", "D+1", "D+2", "D+30", "D+90", "No vencimento", "Baixa", "Outra"]
