# Investimentos & Patrimônio Lutcho e Lidy

Sistema pessoal de gestão e análise patrimonial (uso estritamente pessoal).
Local-first: todos os dados ficam em um banco SQLite na sua máquina
(`patrimonio.db`). Indicadores e cotas vêm **exclusivamente** de fontes
oficiais (Banco Central e CVM). Nenhum dado é inventado — quando uma fonte
está indisponível, o sistema declara a indisponibilidade.

## Missão

Maximizar o rendimento do patrimônio existente por três mecanismos:

1. Expor ativos que rendem abaixo do custo de oportunidade (CDI) e do
   necessário para a meta.
2. Analisar novas aplicações com dados reais antes da decisão.
3. Manter disciplina de acompanhamento com o mínimo de trabalho manual.

**O sistema NÃO é** robô de execução de ordens, previsor de mercado, nem
consultoria de valores mobiliários.

## Requisitos

- Python 3.12+

## Instalação

```bash
pip install -r requirements.txt
```

## Execução

```bash
streamlit run app.py
```

O banco `patrimonio.db` é criado automaticamente no primeiro uso.

### (Opcional) Consultor IA

O Consultor IA usa a API da Anthropic (sem chave, as demais abas funcionam
normalmente). A forma recomendada é um arquivo `.env` na raiz do projeto, que é
carregado automaticamente e **não** é versionado (está no `.gitignore`):

```bash
cp .env.example .env
# edite o .env e preencha ANTHROPIC_API_KEY com sua chave
```

Gere a chave em https://console.anthropic.com/ → **API Keys**. Como alternativa,
você pode exportar a variável no shell antes de rodar:

```bash
export ANTHROPIC_API_KEY="sua-chave"
# opcional: trocar o modelo (default: claude-sonnet-4-6)
export ANTHROPIC_MODEL="claude-sonnet-4-6"
```

### (Opcional) Importar carteira inicial

```bash
python importar_dados_lidiana.py
```

## Arquitetura

```
robo-patrimonio/
├── app.py                      # UI Streamlit — 10 abas
├── atualizar.py                # motor de atualização (CLI para cron/launchd)
├── importar_dados_lidiana.py   # seed da carteira XP (template)
├── requirements.txt
├── patrimonio.db               # criado no 1º uso (gitignore)
├── .cache_mercado/ .cache_cvm/ # caches locais de APIs (gitignore)
├── tests/                      # pytest (parsers + comparação de performance)
└── patrimonio/
    ├── database.py   # persistência + migrações leves (PRAGMA table_info)
    ├── mercado.py    # BCB/SGS: CDI, Selic, IPCA
    ├── cvm.py        # Informe Diário de fundos → cotas/PL + cache em banco
    ├── fundos.py     # camada normalizada de cotas (Cotacao/FonteCVM) + sync
    ├── cadastro_cvm.py # resolução de CNPJ + cadastro de classes CVM
    ├── recomendacao.py # comparação por classe (6/12/24m) + alternativas
    ├── analise.py    # motor de análise por ativo + consolidação + alertas
    ├── projecao.py   # juros compostos, cenários, anos-até-meta
    ├── relatorios.py # evolução patrimonial, proventos, relatório IR
    ├── importador_extrato_conta_xp.py  # Extrato da conta XP (ledger histórico)
    ├── consultor.py  # Consultor IA (Anthropic API + web search)
    └── simulador.py  # paper trading B3 com travas
```

## Fontes de dados (oficiais)

Somente fontes oficiais e gratuitas, sem credenciais de terceiros:

| Dado | Fonte | Endpoint/arquivo | Defasagem |
|---|---|---|---|
| CDI, Selic, IPCA (benchmark) | Banco Central (SGS) | `api.bcb.gov.br` | ~1 dia útil |
| Cota, PL e nº de cotistas de fundos | CVM — Informe Diário FIF | `inf_diario_fi_AAAAMM.zip` | ~1 dia útil |
| Classe/tipo do fundo (para pares) | CVM — Cadastro | `registro_fundo_classe.zip` | atualização periódica |

Limitações conhecidas (nada é tempo real):
- A CVM publica o Informe Diário com ~1 dia útil de atraso; o app trabalha com
  o último pregão disponível.
- **FIDC** não consta do Informe Diário FIF (divulgação própria) — fica manual.
- Renda fixa bancária (CDB/LCI/LCA) e Tesouro Direto não têm CNPJ de fundo.
- O cadastro da CVM **não traz a taxa de administração**; por isso a comparação
  de pares usa desempenho relativo e a taxa do fundo sugerido deve ser conferida.

## Motor de atualização automática

`atualizar.py` sincroniza as cotas dos fundos da carteira no cache local
(`cotas_fundos`, idempotente por CNPJ+data), grava um novo snapshot por ativo a
partir da variação de cota e alerta sobre fundos defasados. Uso:

```bash
python atualizar.py                # sincroniza cotas + snapshots da carteira
python atualizar.py --cadastro     # também atualiza o cadastro de classes CVM
python atualizar.py --universo      # reconstrói o universo de pares por classe (pesado)
python atualizar.py --defasagem 7   # muda o limite (dias úteis) do alerta de atraso
```

Agendamento (rodar em dias úteis, após ~20h, quando o Informe já saiu):

- cron (Linux/macOS):

```cron
0 20 * * 1-5 cd /caminho/NossoPatrimonio && /caminho/.venv/bin/python atualizar.py --silencioso
```

- launchd (macOS), `~/Library/LaunchAgents/com.nossopatrimonio.atualizar.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.nossopatrimonio.atualizar</string>
  <key>ProgramArguments</key>
  <array>
    <string>/caminho/.venv/bin/python</string>
    <string>/caminho/NossoPatrimonio/atualizar.py</string>
    <string>--silencioso</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>20</integer><key>Minute</key><integer>0</integer></dict>
  <key>WorkingDirectory</key><string>/caminho/NossoPatrimonio</string>
</dict>
</plist>
```

Carregue com `launchctl load ~/Library/LaunchAgents/com.nossopatrimonio.atualizar.plist`.

## Comparar & Recomendar (informativo)

A aba **📈 Comparar & Recomendar** compara cada fundo da carteira com a
**mediana da sua classe CVM** em janelas de 6, 12 e 24 meses. Um fundo é
sinalizado como baixo desempenho quando fica **abaixo da mediana em 2 das 3
janelas**; nesse caso o app sugere fundos da mesma classe com desempenho
consistente acima da mediana. Volatilidade e Sharpe entram como informação
complementar (calculados só para os fundos da carteira).

O conteúdo é **estritamente informativo** — não é recomendação de investimento.
O universo de pares é construído a partir do Informe Diário (arquivos grandes),
então essa reconstrução é uma rotina de lote (botão na aba ou `atualizar.py
--universo`), com resultado cacheado em `pares_classe`/`universo_retornos`.

## Testes

```bash
pytest
```

Cobrem os parsers (extrato da conta, extrato de movimentações, helpers da planilha
e do Informe Diário) e a lógica de comparação/recomendação — todos sem rede
(fixtures sintéticas).

**Reset da plataforma:** ao adicionar nova persistência ligada à carteira ou ao
motor de fundos, inclua a tabela em `_TABELAS_ZERAR_*` em `database.py` (hoje
inclui `extrato_conta`, `proventos`, `movimentos`, `snapshots`, `ativos` e o
cache do motor) e, se houver cache em arquivo ou `session_state`, limpe-o em
`app.py` (`_sidebar_zerar_dados`). O cadastro oficial `fundos_cadastro`
(referência CVM) é preservado de propósito.

**Rentabilidade do extrato XP:** a coluna *Rentabilidade* da Posição Detalhada
é gravada em `rent_bruta_extrato` e usada na análise quando presente (retorno
total, incluindo cupons fora da posição — essencial para COE/RF com cupom).
Reimporte o extrato para atualizar ativos já cadastrados.

## Três exports da XP (o que cada um faz no app)

| Export XP | O que traz | Uso no sistema |
|---|---|---|
| **Posição Detalhada** (XLSX/CSV) | Snapshot da carteira: saldo, valor aplicado, rentabilidade | Aba **Importar extrato** — cadastra/atualiza ativos e snapshots |
| **Extrato da conta** (XLSX) | Histórico de movimentações em caixa: compras, TEDs em fundos, resgates, cupons/juros, saldo em conta | Mesma aba, seção inferior — datas de 1ª aplicação, proventos históricos, gráficos de fluxo e saldo em conta no painel |
| **Extrato de movimentações** (XLSX/CSV/PDF) | Layout variável; parser heurístico | Aba **Ativos** (expander) — fallback para datas quando o Extrato da conta não estiver disponível |

Limitações do **Extrato da conta**:
- O saldo em conta é **caixa disponível**, não patrimônio total investido (para isso use snapshots da Posição Detalhada).
- O período exportado pode ter lacunas (ex.: cabeçalho desde 2020, mas movimentos só a partir de 2022).
- Nomes de fundos em TEDs podem vir truncados — o app casa por similaridade e pede conferência antes de gravar.
- O ledger fica em `extrato_conta` e **não altera** `valor_aplicado` (evita duplicar o custo da Posição Detalhada).

Arquivos pessoais exportados da XP devem ficar em `exports_investimentos/` (pasta no `.gitignore`).

## Princípios inegociáveis

1. Nenhum dado inventado — só fontes oficiais (BCB, CVM).
2. Privacidade local-first (SQLite local; nada de nuvem/telemetria).
3. Estimativas sempre rotuladas (IR, projeções, cenários).
4. Sem credenciais de terceiros — automação apenas via dados públicos.
5. Código em PT-BR, com type hints e funções puras onde possível.

## Aviso legal

Uso estritamente pessoal. Não substitui assessoria de investimentos (CVM).
Os Informes de Rendimentos oficiais das instituições sempre prevalecem sobre
qualquer estimativa apresentada aqui.
