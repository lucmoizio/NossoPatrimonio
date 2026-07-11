"""Consultor IA (Anthropic Messages API + web search).

Única exceção ao local-first (§2.2): as perguntas que o usuário aciona
conscientemente são enviadas à API da Anthropic, que pode buscar taxas atuais
em fontes oficiais via a ferramenta de web search. A API é stateless — o
histórico da conversa é mantido pelo chamador (a UI) e reenviado a cada turno.

Requer a variável de ambiente `ANTHROPIC_API_KEY`. Sem ela, `disponivel()`
retorna False e a UI degrada com uma mensagem clara (as demais abas funcionam).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

MODELO_PADRAO = "claude-sonnet-4-6"
MAX_TOKENS = 2048
MAX_BUSCAS_WEB = 5

# System prompt fixo (§4.6): regras inegociáveis do consultor.
SYSTEM_PROMPT = """\
Você é o consultor de investimentos pessoal de um casal brasileiro (Lidiana e
Luciano), perfil conservador-moderado, horizonte de ~10 anos, meta de patrimônio
de R$ 3.000.000 com aportes de ~R$ 100.000/ano. O patrimônio atual é da ordem de
R$ 1,8 milhão, majoritariamente em fundos de renda fixa/crédito privado na XP.

Regras inegociáveis:
1. NUNCA invente dados. Ao citar taxas, cotações ou condições, busque valores
   ATUAIS em fontes oficiais (Banco Central, Tesouro Direto, CVM, B3) usando a
   ferramenta de busca. Se não encontrar, declare a indisponibilidade.
2. SEMPRE compare a aplicação analisada contra o CDI líquido do prazo
   equivalente (o custo de oportunidade de referência do casal).
3. Explicite os riscos: crédito, mercado, liquidez, e a cobertura do FGC
   (R$ 250 mil por CPF por instituição, teto de R$ 1 milhão a cada 4 anos).
4. Detalhe a tributação REAL: tabela regressiva de IR, come-cotas em fundos,
   isenção de LCI/LCA/CRI/CRA/debêntures incentivadas quando aplicável.
5. Considere o perfil conservador-moderado e o horizonte do casal.
6. Encerre SEMPRE com um parecer objetivo: "VALE A PENA", "NÃO VALE A PENA" ou
   "DEPENDE" (explicando de quê depende), seguido da ressalva de que esta
   análise NÃO substitui um assessor de investimentos certificado pela CVM.

Responda em português do Brasil, de forma direta e fundamentada.
"""


@dataclass
class RespostaConsultor:
    """Resposta do consultor (texto + rastros de busca, quando houver)."""

    texto: str
    buscas_realizadas: list[str]
    modelo: str


def disponivel() -> bool:
    """True se há chave de API configurada e o SDK está instalado."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def modelo_atual() -> str:
    """Modelo em uso (env `ANTHROPIC_MODEL` sobrepõe o padrão)."""
    return os.environ.get("ANTHROPIC_MODEL", MODELO_PADRAO)


def perguntar(
    pergunta: str,
    historico: Optional[list[dict]] = None,
    contexto_carteira: Optional[str] = None,
) -> RespostaConsultor:
    """Envia uma pergunta ao consultor, opcionalmente com contexto de carteira.

    `historico` é a lista de mensagens anteriores no formato da Messages API
    ([{'role': 'user'|'assistant', 'content': str}, ...]), mantida pela UI.
    `contexto_carteira` (opcional) é anexado ao início da pergunta do usuário.

    Levanta RuntimeError se o consultor não estiver disponível.
    """
    if not disponivel():
        raise RuntimeError(
            "Consultor IA indisponível: defina ANTHROPIC_API_KEY e instale o "
            "pacote 'anthropic' (pip install -r requirements.txt)."
        )

    import anthropic

    cliente = anthropic.Anthropic()
    mensagens: list[dict] = list(historico or [])

    conteudo_usuario = pergunta
    if contexto_carteira:
        conteudo_usuario = (
            f"Contexto atual da carteira (dados do sistema):\n{contexto_carteira}\n\n"
            f"Pergunta: {pergunta}"
        )
    mensagens.append({"role": "user", "content": conteudo_usuario})

    ferramentas = [
        {
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": MAX_BUSCAS_WEB,
        }
    ]

    resposta = cliente.messages.create(
        model=modelo_atual(),
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=mensagens,
        tools=ferramentas,
    )

    partes_texto: list[str] = []
    buscas: list[str] = []
    for bloco in resposta.content:
        tipo = getattr(bloco, "type", None)
        if tipo == "text":
            partes_texto.append(bloco.text)
        elif tipo == "server_tool_use":
            consulta = getattr(getattr(bloco, "input", None), "get", lambda *_: None)("query")
            if isinstance(bloco.input, dict):
                consulta = bloco.input.get("query")
            if consulta:
                buscas.append(str(consulta))

    return RespostaConsultor(
        texto="\n".join(partes_texto).strip() or "(sem resposta textual)",
        buscas_realizadas=buscas,
        modelo=modelo_atual(),
    )
