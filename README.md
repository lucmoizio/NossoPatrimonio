# Robô Patrimônio

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

O Consultor IA usa a API da Anthropic. Defina a variável de ambiente antes de
rodar (sem ela, as demais abas funcionam normalmente):

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
├── app.py                      # UI Streamlit — 7 abas
├── importar_dados_lidiana.py   # seed da carteira XP (template)
├── requirements.txt
├── patrimonio.db               # criado no 1º uso (gitignore)
├── .cache_mercado/ .cache_cvm/ # caches locais de APIs (gitignore)
└── patrimonio/
    ├── database.py   # persistência + migrações leves (PRAGMA table_info)
    ├── mercado.py    # BCB/SGS: CDI, Selic, IPCA
    ├── cvm.py        # informes diários de fundos → atualização automática
    ├── analise.py    # motor de análise por ativo + consolidação + alertas
    ├── projecao.py   # juros compostos, cenários, anos-até-meta
    ├── relatorios.py # evolução patrimonial, proventos, relatório IR
    ├── consultor.py  # Consultor IA (Anthropic API + web search)
    └── simulador.py  # paper trading B3 com travas
```

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
