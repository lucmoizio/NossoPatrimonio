"""Investimentos & Patrimônio Lutcho e Lidy — interface Streamlit (8 abas).

Uso pessoal. Local-first: os dados ficam em `patrimonio.db` na sua máquina.
Indicadores e cotas vêm apenas de fontes oficiais (BCB, CVM); estimativas são
sempre rotuladas.

Execução:  streamlit run app.py
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from patrimonio import (
    analise,
    cadastro_cvm,
    consultor,
    cvm,
    database,
    importador_xp,
    importador_xp_planilha,
    mercado,
    projecao,
    relatorios,
    simulador,
)
from patrimonio.dominio import CATEGORIAS, LIQUIDEZ


# --------------------------------------------------------------------------- #
# Helpers de formatação pt-BR
# --------------------------------------------------------------------------- #
def brl(valor: float | None) -> str:
    """Formata um número como moeda brasileira: 1234.5 → 'R$ 1.234,50'."""
    if valor is None:
        return "—"
    inteiro = f"{valor:,.2f}"
    inteiro = inteiro.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {inteiro}"


def pct(fracao: float | None, casas: int = 2) -> str:
    """Formata uma fração decimal como percentual pt-BR: 0.1365 → '13,65%'."""
    if fracao is None:
        return "—"
    texto = f"{fracao * 100:.{casas}f}"
    return texto.replace(".", ",") + "%"


# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #
st.set_page_config(
    page_title="Investimentos & Patrimônio Lutcho e Lidy", page_icon="💼", layout="wide"
)
database.inicializar()


@st.cache_data(ttl=3600, show_spinner=False)
def _indicadores_cache() -> dict:
    """Cacheia os indicadores oficiais por 1h (a fonte já tem cache de 12h)."""
    ind = mercado.indicadores_atuais()
    return {
        "selic": ind.selic_meta_aa,
        "cdi": ind.cdi_aa,
        "ipca": ind.ipca_12m,
        "consultado_em": ind.consultado_em,
        "fonte": ind.fonte,
    }


def _mapa_titulares() -> dict[str, int | None]:
    titulares = database.listar_titulares()
    mapa: dict[str, int | None] = {"Consolidado": None}
    for t in titulares:
        mapa[t["nome"]] = int(t["id"])
    return mapa


# --------------------------------------------------------------------------- #
# Sidebar: filtro + indicadores oficiais
# --------------------------------------------------------------------------- #
def render_sidebar() -> int | None:
    st.sidebar.title("💼 Investimentos & Patrimônio")
    st.sidebar.caption("Lutcho & Lidy")
    mapa = _mapa_titulares()
    escolha = st.sidebar.radio("Visão", list(mapa.keys()), index=0)
    titular_id = mapa[escolha]

    st.sidebar.divider()
    st.sidebar.subheader("Indicadores oficiais")
    try:
        ind = _indicadores_cache()
        st.sidebar.metric("Selic meta (a.a.)", pct((ind["selic"] or 0) / 100) if ind["selic"] is not None else "—")
        st.sidebar.metric("CDI (a.a.)", pct((ind["cdi"] or 0) / 100) if ind["cdi"] is not None else "—")
        st.sidebar.metric("IPCA (12m)", pct((ind["ipca"] or 0) / 100) if ind["ipca"] is not None else "—")
        st.sidebar.caption(f"Fonte: {ind['fonte']}")
        st.sidebar.caption(f"Consulta: {ind['consultado_em']}")
    except mercado.ErroDadosMercado as exc:
        st.sidebar.warning(f"Indicadores indisponíveis: {exc}")

    st.sidebar.divider()
    _sidebar_zerar_dados()

    st.sidebar.divider()
    st.sidebar.caption(
        "Uso pessoal. Não substitui assessoria CVM. Estimativas são rotuladas."
    )
    return titular_id


def _sidebar_zerar_dados() -> None:
    """Seção de reset: apaga os dados da plataforma após dupla confirmação."""
    with st.sidebar.expander("⚠️ Zerar dados", expanded=False):
        st.caption(
            "Apaga toda a carteira (ativos, snapshots, aportes/resgates e "
            "proventos) para recomeçar as importações do zero. Os titulares "
            "são mantidos. Ação irreversível."
        )
        incluir_metas = st.checkbox("Apagar também metas", value=True, key="zerar_metas")
        incluir_sim = st.checkbox("Apagar também simulador", value=True, key="zerar_sim")
        confirma = st.checkbox(
            "Entendo que esta ação é irreversível", value=False, key="zerar_confirma"
        )
        if st.button(
            "🗑️ Apagar todos os dados", type="primary", disabled=not confirma
        ):
            removidos = database.zerar_dados(
                incluir_metas=incluir_metas, incluir_simulador=incluir_sim
            )
            total = sum(removidos.values())
            # Limpa estados de importação e caches em memória.
            st.session_state.pop("import_estado", None)
            st.cache_data.clear()
            st.success(
                f"Dados zerados: {total} registros removidos "
                f"({', '.join(f'{k}={v}' for k, v in removidos.items() if v)} )."
                if total
                else "Não havia dados para apagar."
            )
            st.rerun()


# --------------------------------------------------------------------------- #
# Aba: Painel
# --------------------------------------------------------------------------- #
def aba_painel(titular_id: int | None) -> None:
    st.header("📊 Painel consolidado")
    with st.spinner("Analisando carteira..."):
        cons = analise.consolidar(titular_id=titular_id)

    if not cons.analises:
        st.info("Nenhum ativo cadastrado ainda. Vá até a aba 📋 Ativos para começar.")
        return

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Patrimônio atual", brl(cons.total_atual))
    c2.metric("Total aplicado", brl(cons.total_aplicado))
    c3.metric("Ganho bruto", brl(cons.ganho), delta=pct(cons.rent_media_ponderada))
    c4.metric("Alertas", str(len(cons.alertas)))

    col_a, col_b = st.columns(2)
    with col_a:
        st.subheader("Alocação por categoria")
        if cons.alocacao_categoria:
            fig = px.pie(
                names=list(cons.alocacao_categoria.keys()),
                values=list(cons.alocacao_categoria.values()),
                hole=0.4,
            )
            fig.update_layout(margin=dict(t=10, b=10, l=10, r=10))
            st.plotly_chart(fig, use_container_width=True)

    with col_b:
        st.subheader("Rentabilidade do ativo × CDI do período")
        dados_barras = [
            {
                "Ativo": a.nome,
                "Rent. bruta": a.rent_bruta * 100,
                "CDI do período": (a.cdi_periodo or 0) * 100,
            }
            for a in cons.analises
            if a.cdi_periodo is not None
        ]
        if dados_barras:
            dfb = pd.DataFrame(dados_barras).melt(
                id_vars="Ativo", var_name="Série", value_name="%"
            )
            fig = px.bar(dfb, x="Ativo", y="%", color="Série", barmode="group")
            fig.update_layout(margin=dict(t=10, b=10, l=10, r=10))
            st.plotly_chart(fig, use_container_width=True)
            total = len(cons.analises)
            mostrados = len(dados_barras)
            if mostrados < total:
                st.caption(
                    f"Mostrando {mostrados} de {total} ativos. Os demais não têm "
                    "data de aplicação válida para delimitar o CDI do período — "
                    "informe a data da 1ª aplicação na aba 📋 Ativos para incluí-los."
                )
        else:
            st.caption(
                "Nenhum ativo tem data de aplicação válida para calcular o CDI do "
                "período. Informe a data da 1ª aplicação na aba 📋 Ativos."
            )

    st.subheader("Evolução patrimonial")
    df_evo = relatorios.evolucao_patrimonial(titular_id=titular_id)
    if not df_evo.empty and len(df_evo) > 1:
        fig = px.area(df_evo, x="data", y="total")
        fig.update_layout(margin=dict(t=10, b=10, l=10, r=10), yaxis_title="R$")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.caption("Registre snapshots em datas diferentes para ver a evolução histórica.")

    st.subheader("Tabela analítica por ativo")
    linhas = []
    for a in cons.analises:
        linhas.append(
            {
                "Ativo": a.nome,
                "Titular": a.titular,
                "Categoria": a.categoria,
                "Aplicado": brl(a.valor_aplicado),
                "Atual": brl(a.valor_atual),
                "Rent. bruta": pct(a.rent_bruta),
                "% do CDI": pct(a.pct_cdi) if a.pct_cdi is not None else "—",
                "Rent. real": pct(a.rent_real) if a.rent_real is not None else "—",
                "IR estimado*": brl(a.ir_estimado),
            }
        )
    st.dataframe(pd.DataFrame(linhas), use_container_width=True, hide_index=True)
    st.caption("*IR estimado pela tabela regressiva (Lei 11.033/2004). O Informe de Rendimentos oficial prevalece.")

    if cons.alertas:
        st.subheader("Alertas")
        icones = {"revisao": "🔴", "atencao": "🟡", "info": "🔵"}
        for nome, alerta in cons.alertas:
            st.write(f"{icones.get(alerta.nivel, '•')} **{nome}** — {alerta.mensagem}")


# --------------------------------------------------------------------------- #
# Aba: Ativos
# --------------------------------------------------------------------------- #
def aba_ativos(titular_id: int | None) -> None:
    st.header("📋 Ativos")
    mapa = _mapa_titulares()
    titulares_nomes = [n for n in mapa if n != "Consolidado"]

    with st.expander("➕ Cadastrar novo ativo", expanded=False):
        with st.form("form_novo_ativo", clear_on_submit=True):
            c1, c2 = st.columns(2)
            titular_nome = c1.selectbox("Titular", titulares_nomes)
            nome = c2.text_input("Nome do ativo")
            c3, c4 = st.columns(2)
            categoria = c3.selectbox("Categoria", CATEGORIAS)
            liquidez = c4.selectbox("Liquidez", LIQUIDEZ)
            c5, c6, c7 = st.columns(3)
            valor_aplicado = c5.number_input("Valor aplicado (R$)", min_value=0.0, step=100.0)
            taxa_adm = c6.number_input("Taxa adm. (% a.a.)", min_value=0.0, step=0.1)
            data_aplicacao = c7.date_input("Data 1ª aplicação", value=date.today())
            c8, c9 = st.columns(2)
            cnpj = c8.text_input("CNPJ do fundo (opcional)")
            isento = c9.checkbox("Isento de IR")
            come_cotas = c9.checkbox("Sujeito a come-cotas")
            observacoes = st.text_area("Observações", height=68)
            enviado = st.form_submit_button("Cadastrar ativo")
            if enviado:
                if not nome.strip():
                    st.error("Informe o nome do ativo.")
                else:
                    database.inserir_ativo(
                        titular_id=mapa[titular_nome],
                        nome=nome.strip(),
                        categoria=categoria,
                        liquidez=liquidez,
                        taxa_adm_aa=taxa_adm or None,
                        isento_ir=isento,
                        come_cotas=come_cotas,
                        data_aplicacao=data_aplicacao.isoformat(),
                        valor_aplicado=valor_aplicado,
                        observacoes=observacoes or None,
                        cnpj=cnpj.strip() or None,
                    )
                    st.success(f"Ativo '{nome}' cadastrado.")
                    st.rerun()

    ativos = database.listar_ativos(titular_id=titular_id, apenas_ativos=False)
    if not ativos:
        st.info("Nenhum ativo cadastrado.")
        return

    st.subheader("Carteira cadastrada")
    st.caption(
        "Edite direto na tabela e clique em salvar. A **data de aplicação** é o que "
        "permite comparar o ativo com o CDI do período — fundos importados da "
        "planilha vêm com a data de referência; ajuste para a data real da 1ª "
        "aplicação para que apareçam no gráfico do painel."
    )
    orig = {int(a["id"]): a for a in ativos}
    df = pd.DataFrame(
        [
            {
                "ID": int(a["id"]),
                "Titular": a["titular_nome"],
                "Nome": a["nome"],
                "Categoria": a["categoria"],
                "CNPJ": a["cnpj"] or "",
                "Data aplicação": a["data_aplicacao"] or "",
                "Aplicado": float(a["valor_aplicado"] or 0.0),
                "Isento IR": bool(a["isento_ir"]),
                "Ativo": bool(a["ativo"]),
            }
            for a in ativos
        ]
    )
    editado = st.data_editor(
        df,
        key="editor_ativos",
        use_container_width=True,
        hide_index=True,
        column_config={
            "ID": st.column_config.NumberColumn("ID", disabled=True),
            "Titular": st.column_config.TextColumn("Titular", disabled=True),
            "Nome": st.column_config.TextColumn("Nome", disabled=True),
            "Categoria": st.column_config.SelectboxColumn("Categoria", options=CATEGORIAS),
            "CNPJ": st.column_config.TextColumn("CNPJ"),
            "Data aplicação": st.column_config.TextColumn(
                "Data aplicação", help="Formato ISO aaaa-mm-dd. Delimita o CDI do período."
            ),
            "Aplicado": st.column_config.NumberColumn("Aplicado (R$)", format="%.2f"),
            "Isento IR": st.column_config.CheckboxColumn("Isento IR"),
            "Ativo": st.column_config.CheckboxColumn("Ativo"),
        },
    )
    if st.button("💾 Salvar alterações da carteira", type="primary"):
        alterados = 0
        erros: list[str] = []
        for _, linha in editado.iterrows():
            aid = int(linha["ID"])
            a = orig.get(aid)
            if a is None:
                continue
            campos: dict[str, object] = {}

            data_nova = str(linha["Data aplicação"]).strip() or None
            if data_nova is not None:
                try:
                    date.fromisoformat(data_nova)
                except ValueError:
                    erros.append(f"#{aid} {a['nome']}: data '{data_nova}' inválida (use aaaa-mm-dd).")
                    continue
            if data_nova != (a["data_aplicacao"] or None):
                campos["data_aplicacao"] = data_nova

            cnpj_novo = "".join(ch for ch in str(linha["CNPJ"]) if ch.isdigit()) or None
            if cnpj_novo != (a["cnpj"] or None):
                campos["cnpj"] = cnpj_novo
            if str(linha["Categoria"]) != a["categoria"]:
                campos["categoria"] = str(linha["Categoria"])
            if abs(float(linha["Aplicado"]) - float(a["valor_aplicado"] or 0.0)) > 0.001:
                campos["valor_aplicado"] = float(linha["Aplicado"])
            if bool(linha["Isento IR"]) != bool(a["isento_ir"]):
                campos["isento_ir"] = int(bool(linha["Isento IR"]))
            if bool(linha["Ativo"]) != bool(a["ativo"]):
                campos["ativo"] = int(bool(linha["Ativo"]))

            if campos:
                database.atualizar_ativo(aid, campos)
                alterados += 1

        for e in erros:
            st.error(e)
        if alterados:
            st.success(f"{alterados} ativo(s) atualizado(s).")
            st.cache_data.clear()
            st.rerun()
        elif not erros:
            st.info("Nenhuma alteração detectada.")

    st.subheader("Remover ativo")
    opcoes = {f"#{a['id']} — {a['nome']} ({a['titular_nome']})": int(a["id"]) for a in ativos}
    escolha = st.selectbox("Selecione", list(opcoes.keys()))
    col1, col2 = st.columns(2)
    if col1.button("Desativar (preserva histórico)"):
        database.desativar_ativo(opcoes[escolha])
        st.success("Ativo desativado.")
        st.rerun()
    if col2.button("Remover definitivamente", type="secondary"):
        database.remover_ativo(opcoes[escolha])
        st.warning("Ativo removido definitivamente.")
        st.rerun()


# --------------------------------------------------------------------------- #
# Aba: Atualizar valores
# --------------------------------------------------------------------------- #
def aba_atualizar(titular_id: int | None) -> None:
    st.header("📝 Atualizar valores")
    ativos = database.listar_ativos(titular_id=titular_id)
    if not ativos:
        st.info("Cadastre ativos primeiro.")
        return

    st.subheader("Atualização automática via CVM (fundos com CNPJ)")
    st.caption(
        "Estima o valor atual pela variação da cota oficial (Informe Diário CVM) "
        "desde o último snapshot. FIDCs não constam desse informe e ficam manuais."
    )
    fundos = [a for a in ativos if a["cnpj"]]
    if fundos and st.button("🔄 Atualizar fundos pela CVM"):
        prog = st.progress(0.0)
        resultados = []
        for i, a in enumerate(fundos, start=1):
            snap = database.ultimo_snapshot(int(a["id"]))
            if snap is None:
                resultados.append((a["nome"], "sem snapshot de referência — registre um valor manual primeiro"))
            else:
                try:
                    est = cvm.novo_valor_estimado(a["cnpj"], snap["data"], float(snap["valor"]))
                    if est:
                        database.registrar_snapshot(int(a["id"]), date.today().isoformat(), est["valor_novo"])
                        resultados.append(
                            (a["nome"], f"{brl(est['valor_novo'])} (cota {est['data_cota_atual']}, var {pct(est['variacao'])})")
                        )
                    else:
                        resultados.append((a["nome"], "cota indisponível na CVM (ex.: FIDC) — atualize manualmente"))
                except cvm.ErroDadosCVM as exc:
                    resultados.append((a["nome"], f"erro CVM: {exc}"))
            prog.progress(i / len(fundos))
        st.write("**Resultado da atualização:**")
        for nome, msg in resultados:
            st.write(f"- **{nome}**: {msg}")
        st.rerun()

    st.divider()
    st.subheader("Snapshot manual de valor")
    opcoes = {f"{a['nome']} ({a['titular_nome']})": int(a["id"]) for a in ativos}
    with st.form("form_snapshot", clear_on_submit=True):
        escolha = st.selectbox("Ativo", list(opcoes.keys()))
        c1, c2 = st.columns(2)
        data_ref = c1.date_input("Data de referência", value=date.today())
        valor = c2.number_input("Valor bruto (R$)", min_value=0.0, step=100.0)
        if st.form_submit_button("Registrar snapshot") and valor > 0:
            database.registrar_snapshot(opcoes[escolha], data_ref.isoformat(), valor)
            st.success("Snapshot registrado.")
            st.rerun()

    st.divider()
    st.subheader("Movimentos (aporte / resgate)")
    st.caption("Movimentos ajustam automaticamente o valor aplicado do ativo.")
    with st.form("form_movimento", clear_on_submit=True):
        escolha_m = st.selectbox("Ativo ", list(opcoes.keys()), key="mov_ativo")
        c1, c2, c3 = st.columns(3)
        tipo = c1.selectbox("Tipo", ["aporte", "resgate"])
        data_mov = c2.date_input("Data", value=date.today(), key="mov_data")
        valor_mov = c3.number_input("Valor (R$)", min_value=0.0, step=100.0, key="mov_valor")
        if st.form_submit_button("Registrar movimento") and valor_mov > 0:
            database.registrar_movimento(opcoes[escolha_m], data_mov.isoformat(), tipo, valor_mov)
            st.success(f"{tipo.capitalize()} registrado.")
            st.rerun()


# --------------------------------------------------------------------------- #
# Aba: Metas e projeções
# --------------------------------------------------------------------------- #
def aba_metas(titular_id: int | None) -> None:
    st.header("🎯 Metas e projeções")
    st.caption("Projeções são estimativas. Os cenários derivam do CDI oficial atual.")

    cons = analise.consolidar(titular_id=titular_id)
    valor_inicial = cons.total_atual

    with st.form("form_meta"):
        c1, c2 = st.columns(2)
        valor_alvo = c1.number_input("Meta de patrimônio (R$)", min_value=0.0, value=3_000_000.0, step=50_000.0)
        prazo = c2.number_input("Prazo (anos)", min_value=1, value=10, step=1)
        c3, c4 = st.columns(2)
        aporte = c3.number_input("Aporte anual (R$)", min_value=0.0, value=100_000.0, step=10_000.0)
        c4.metric("Patrimônio inicial (atual)", brl(valor_inicial))
        simular = st.form_submit_button("Projetar cenários")

    if simular or valor_alvo > 0:
        cdi_liq = projecao.cdi_liquido_atual()
        if cdi_liq is None:
            st.warning(
                "CDI oficial indisponível no momento — não é possível projetar os "
                "cenários sem inventar taxas. Tente novamente mais tarde."
            )
            return

        st.info(f"CDI líquido de referência (a.a.): {pct(cdi_liq)} (bruto menos 15% de IR de longo prazo).")
        cenarios = projecao.cenarios_padrao(valor_inicial, aporte, int(prazo), valor_alvo, cdi_liq)

        cols = st.columns(len(cenarios))
        for col, cen in zip(cols, cenarios):
            col.metric(
                cen.nome.split(" (")[0],
                brl(cen.valor_final),
                delta=(f"meta em {cen.anos_ate_meta} anos" if cen.anos_ate_meta is not None else "meta não atingida"),
            )

        fig = go.Figure()
        for cen in cenarios:
            fig.add_trace(
                go.Scatter(
                    x=[p.ano for p in cen.trajetoria],
                    y=[p.valor for p in cen.trajetoria],
                    mode="lines+markers",
                    name=cen.nome,
                )
            )
        fig.add_hline(y=valor_alvo, line_dash="dash", annotation_text="Meta")
        fig.update_layout(xaxis_title="Ano", yaxis_title="R$", margin=dict(t=10, b=10, l=10, r=10))
        st.plotly_chart(fig, use_container_width=True)

        dobrar = projecao.tempo_para_dobrar(cenarios[1].taxa_aa)
        if dobrar:
            st.caption(f"No cenário base, o capital dobra em ~{dobrar} anos (sem novos aportes).")


# --------------------------------------------------------------------------- #
# Aba: Consultor IA
# --------------------------------------------------------------------------- #
def aba_consultor(titular_id: int | None) -> None:
    st.header("🧠 Consultor IA")
    if not consultor.disponivel():
        st.warning(
            "Consultor IA indisponível. Defina a variável de ambiente "
            "`ANTHROPIC_API_KEY` e instale as dependências para habilitá-lo. "
            "As demais abas funcionam normalmente."
        )
        return

    st.caption(
        f"Modelo: {consultor.modelo_atual()}. As perguntas são enviadas à API da "
        "Anthropic (única exceção ao local-first). O consultor busca taxas atuais "
        "em fontes oficiais e não substitui um assessor CVM."
    )

    incluir_contexto = st.checkbox("Incluir minha carteira consolidada no contexto", value=False)

    if "chat_consultor" not in st.session_state:
        st.session_state.chat_consultor = []

    for msg in st.session_state.chat_consultor:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    pergunta = st.chat_input("Pergunte sobre uma aplicação, taxa, realocação...")
    if pergunta:
        with st.chat_message("user"):
            st.markdown(pergunta)

        contexto = None
        if incluir_contexto:
            cons = analise.consolidar(titular_id=titular_id)
            linhas = [
                f"- {a.nome} ({a.categoria}): atual {brl(a.valor_atual)}, "
                f"rent. {pct(a.rent_bruta)}, %CDI {pct(a.pct_cdi) if a.pct_cdi else 'n/d'}"
                for a in cons.analises
            ]
            contexto = (
                f"Patrimônio atual {brl(cons.total_atual)}, aplicado {brl(cons.total_aplicado)}.\n"
                + "\n".join(linhas)
            )

        with st.chat_message("assistant"):
            with st.spinner("Consultando fontes oficiais..."):
                try:
                    resp = consultor.perguntar(
                        pergunta,
                        historico=st.session_state.chat_consultor,
                        contexto_carteira=contexto,
                    )
                    st.markdown(resp.texto)
                    if resp.buscas_realizadas:
                        st.caption("Buscas: " + "; ".join(resp.buscas_realizadas))
                    texto_resposta = resp.texto
                except Exception as exc:  # erro de API/rede: degrada com clareza
                    texto_resposta = f"Erro ao consultar: {exc}"
                    st.error(texto_resposta)

        st.session_state.chat_consultor.append({"role": "user", "content": pergunta})
        st.session_state.chat_consultor.append({"role": "assistant", "content": texto_resposta})

    if st.session_state.chat_consultor and st.button("Limpar conversa"):
        st.session_state.chat_consultor = []
        st.rerun()


# --------------------------------------------------------------------------- #
# Aba: Simulador
# --------------------------------------------------------------------------- #
def aba_simulador() -> None:
    st.header("🎮 Simulador (paper trading B3)")
    st.caption(
        "Filosofia core-satellite: a carteira real é o núcleo conservador; aqui "
        "você testa ideias com dinheiro fictício. O teste da verdade é bater o CDI."
    )

    cfg = database.obter_sim_config()
    with st.expander("⚙️ Configurar simulador", expanded=cfg is None):
        with st.form("form_sim_cfg"):
            c1, c2 = st.columns(2)
            saldo = c1.number_input(
                "Saldo inicial fictício (R$)", min_value=0.0,
                value=float(cfg["saldo_inicial"]) if cfg else 100_000.0, step=1000.0,
            )
            data_inicio = c2.date_input(
                "Data de início", value=date.fromisoformat(cfg["data_inicio"]) if cfg else date.today()
            )
            c3, c4 = st.columns(2)
            custo = c3.number_input("Custo por ordem (%)", min_value=0.0, value=float(cfg["custo_pct"]) if cfg else 0.03, step=0.01)
            trava = c4.number_input("Trava de perda (%)", min_value=0.0, value=float(cfg["limite_perda_pct"]) if cfg else 20.0, step=1.0)
            if st.form_submit_button("Salvar configuração"):
                simulador.configurar(saldo, data_inicio.isoformat(), custo, trava)
                st.success("Simulador configurado.")
                st.rerun()

    if cfg is None:
        st.info("Configure o simulador acima para começar.")
        return

    st.subheader("Nova ordem")
    with st.form("form_ordem", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        codigo = c1.text_input("Ticker B3 (ex.: PETR4, BOVA11)")
        tipo = c2.selectbox("Tipo", ["compra", "venda"])
        qtd = c3.number_input("Quantidade", min_value=0.0, step=1.0)
        enviar = st.form_submit_button("Enviar ordem (a mercado)")
        if enviar and codigo.strip() and qtd > 0:
            try:
                if tipo == "compra":
                    r = simulador.comprar(codigo, qtd)
                else:
                    r = simulador.vender(codigo, qtd)
                st.success(f"{tipo.capitalize()} de {qtd} {r['ticker']} a {brl(r['preco'])} (custos {brl(r['custos'])}).")
                st.rerun()
            except simulador.ErroSimulador as exc:
                st.error(str(exc))

    res = simulador.resultado()
    if res:
        st.subheader("Placar")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Patrimônio", brl(res.patrimonio), delta=pct(res.rent_pct / 100))
        c2.metric("Caixa", brl(res.caixa))
        c3.metric("Em posições", brl(res.valor_posicoes))
        c4.metric(
            "vs CDI",
            f"{res.vs_cdi_pp:+.2f} p.p." if res.vs_cdi_pp is not None else "—",
            delta=(f"CDI {res.cdi_periodo_pct:.2f}%" if res.cdi_periodo_pct is not None else None),
        )
        if res.trava_atingida:
            st.error(f"🔴 Trava de perda de {res.limite_perda_pct:.0f}% atingida!")
        for aviso in res.avisos:
            st.warning(aviso)
        if res.posicoes:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Ticker": p["ticker"],
                            "Qtd": p["quantidade"],
                            "Preço médio": brl(p["preco_medio"]),
                            "Preço atual": brl(p["preco_atual"]),
                            "Valor": brl(p["valor"]),
                            "Resultado": brl(p["resultado"]),
                        }
                        for p in res.posicoes
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )

    if st.button("Reiniciar simulador (zera tudo)"):
        simulador.reiniciar()
        st.warning("Simulador reiniciado.")
        st.rerun()


# --------------------------------------------------------------------------- #
# Aba: Proventos & IR
# --------------------------------------------------------------------------- #
def aba_proventos_ir(titular_id: int | None) -> None:
    st.header("💰 Proventos & IR")
    ativos = database.listar_ativos(titular_id=titular_id, apenas_ativos=False)
    if not ativos:
        st.info("Cadastre ativos primeiro.")
        return
    opcoes = {f"{a['nome']} ({a['titular_nome']})": int(a["id"]) for a in ativos}

    st.subheader("Registrar provento")
    with st.form("form_provento", clear_on_submit=True):
        c1, c2 = st.columns(2)
        escolha = c1.selectbox("Ativo", list(opcoes.keys()))
        tipo = c2.selectbox("Tipo", ["Dividendo", "JCP", "Rendimento", "Juros", "Amortização", "Outro"])
        c3, c4 = st.columns(2)
        data_ref = c3.date_input("Data", value=date.today())
        valor = c4.number_input("Valor (R$)", min_value=0.0, step=10.0)
        if st.form_submit_button("Registrar") and valor > 0:
            database.registrar_provento(opcoes[escolha], data_ref.isoformat(), tipo, valor)
            st.success("Provento registrado.")
            st.rerun()

    ano = st.number_input("Ano-base", min_value=2000, max_value=2100, value=date.today().year, step=1)

    st.subheader("Renda passiva")
    resumo = relatorios.resumo_proventos(int(ano), titular_id=titular_id)
    c1, c2 = st.columns(2)
    c1.metric("Total no ano", brl(resumo.total_ano))
    c2.metric("Média mensal", brl(resumo.media_mensal))
    if resumo.por_mes:
        dfm = pd.DataFrame({"Mês": list(resumo.por_mes.keys()), "R$": list(resumo.por_mes.values())})
        st.plotly_chart(px.bar(dfm, x="Mês", y="R$"), use_container_width=True)

    st.subheader("Relatório de IR (auxílio de preenchimento)")
    rel = relatorios.relatorio_ir(int(ano), titular_id=titular_id)
    st.info(rel.aviso)
    if rel.bens_direitos:
        df_bd = pd.DataFrame(
            [
                {
                    "Ativo": b.nome,
                    "Titular": b.titular,
                    "Categoria": b.categoria,
                    "CNPJ": b.cnpj or "—",
                    f"Situação em 31/12/{rel.ano_anterior}": brl(b.situacao_ano_anterior),
                    f"Situação em 31/12/{rel.ano_base}": brl(b.situacao_ano_base),
                }
                for b in rel.bens_direitos
            ]
        )
        st.dataframe(df_bd, use_container_width=True, hide_index=True)
    else:
        st.caption("Sem posições registradas em 31/12 dos anos considerados.")

    if rel.proventos_por_tipo:
        st.write("**Proventos do ano por tipo:**")
        for tipo_p, total in rel.proventos_por_tipo.items():
            st.write(f"- {tipo_p}: {brl(total)}")


# --------------------------------------------------------------------------- #
# Aba: Importar extrato
# --------------------------------------------------------------------------- #
def aba_importar() -> None:
    st.header("📥 Importar extrato (XP)")
    st.caption(
        "Envie a **Posição Detalhada** (XLSX/CSV — recomendado, traz o valor "
        "aplicado) ou o relatório de performance (PDF). O sistema lê os ativos, "
        "resolve o CNPJ dos fundos pela CVM e grava após a sua conferência."
    )

    mapa = _mapa_titulares()
    titulares_nomes = [n for n in mapa if n != "Consolidado"]

    arquivo = st.file_uploader(
        "Arquivo da XP (Posição Detalhada .xlsx/.csv ou XPerformance .pdf)",
        type=["xlsx", "xls", "csv", "pdf"],
    )
    if arquivo is None:
        st.info(
            "Aguardando o envio. Dica: no app/site da XP, exporte a "
            "'Posição Detalhada' em Excel — é o formato mais completo."
        )
        return

    resolver = st.checkbox(
        "Resolver CNPJ automaticamente pela CVM (requer internet)", value=True
    )

    chave = f"{arquivo.name}:{arquivo.size}:{resolver}"
    estado = st.session_state.get("import_estado")
    if estado is None or estado.get("chave") != chave:
        nome_lower = arquivo.name.lower()
        eh_pdf = nome_lower.endswith(".pdf")
        try:
            with st.spinner("Lendo o arquivo..."):
                if eh_pdf:
                    resultado = importador_xp.importar(arquivo.getvalue())
                else:
                    resultado = importador_xp_planilha.importar(
                        arquivo.getvalue(), nome_arquivo=arquivo.name
                    )
        except importador_xp.ErroImportacaoXP as exc:
            st.error(f"Não foi possível interpretar o arquivo: {exc}")
            return

        resolucoes = None
        if resolver and resultado.metadados.data_referencia:
            with st.spinner("Resolvendo CNPJs na CVM (cadastro + Informe Diário)..."):
                try:
                    resolucoes = cadastro_cvm.resolver_carteira(
                        resultado.posicoes, resultado.metadados.data_referencia
                    )
                except Exception as exc:  # rede/cadastro: degrada com clareza
                    st.warning(f"Resolução de CNPJ indisponível: {exc}")
                    resolucoes = None

        estado = {"chave": chave, "resultado": resultado, "resolucoes": resolucoes}
        st.session_state["import_estado"] = estado

    resultado = estado["resultado"]
    resolucoes = estado["resolucoes"]
    meta = resultado.metadados

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Conta", meta.conta or "—")
    c2.metric("Data de referência", meta.data_referencia or "—")
    c3.metric("Ativos", str(len(resultado.posicoes)))
    c4.metric("Soma / Total", brl(resultado.soma_saldos))
    if meta.patrimonio_total is not None:
        if resultado.confere_total:
            st.success(f"Soma dos ativos confere com o patrimônio total: {brl(meta.patrimonio_total)}.")
        else:
            st.warning(
                f"Soma ({brl(resultado.soma_saldos)}) diverge do total do relatório "
                f"({brl(meta.patrimonio_total)}). Revise antes de gravar."
            )

    col_t, col_d = st.columns(2)
    titular_default = "Lidiana" if "Lidiana" in titulares_nomes else titulares_nomes[0]
    titular_nome = col_t.selectbox("Titular destes ativos", titulares_nomes, index=titulares_nomes.index(titular_default))
    data_ref = col_d.text_input("Data de referência (snapshot)", value=meta.data_referencia or date.today().isoformat())

    # Monta a tabela de conferência.
    resol_por_nome = {}
    if resolucoes:
        resol_por_nome = {r.nome_extrato: r for r in resolucoes}

    linhas = []
    for p in resultado.posicoes:
        r = resol_por_nome.get(p.nome)
        valor_aplicado = p.valor_aplicado if p.valor_aplicado is not None else round(p.saldo_bruto, 2)
        data_aplic = p.data_aplicacao or data_ref
        linhas.append(
            {
                "incluir": True,
                "nome": p.nome,
                "categoria": p.categoria,
                "cnpj": cadastro_cvm.formatar_cnpj(r.cnpj) if r and r.cnpj else "",
                "status_cnpj": r.rotulo_status if r else ("—" if not resolver else "não resolvido"),
                "saldo_bruto": round(p.saldo_bruto, 2),
                "quantidade": p.quantidade,
                "valor_aplicado": valor_aplicado,
                "data_aplicacao": data_aplic,
                "taxa_adm_aa": 0.0,
                "isento_ir": False,
                "come_cotas": p.categoria in ("Renda Fixa - Pós-fixado", "Crédito Privado", "Fundo Multimercado"),
            }
        )
    df = pd.DataFrame(linhas)

    tem_valor_aplicado = any(p.valor_aplicado is not None for p in resultado.posicoes)
    st.caption(
        "Confira e ajuste. O valor aplicado veio do arquivo."
        if tem_valor_aplicado
        else "Confira e ajuste. `valor_aplicado` assume o saldo atual por padrão "
        "(rentabilidade inicia em 0 até você informar o custo real de aquisição)."
    )
    editado = st.data_editor(
        df,
        key="editor_import",
        use_container_width=True,
        hide_index=True,
        column_config={
            "incluir": st.column_config.CheckboxColumn("Incluir"),
            "nome": st.column_config.TextColumn("Ativo", width="large"),
            "categoria": st.column_config.SelectboxColumn("Categoria", options=CATEGORIAS),
            "cnpj": st.column_config.TextColumn("CNPJ"),
            "status_cnpj": st.column_config.TextColumn("Status CNPJ", disabled=True),
            "saldo_bruto": st.column_config.NumberColumn("Saldo bruto", format="%.2f"),
            "quantidade": st.column_config.NumberColumn("Qtd. cotas", format="%.6f"),
            "valor_aplicado": st.column_config.NumberColumn("Valor aplicado", format="%.2f"),
            "data_aplicacao": st.column_config.TextColumn("Data aplicação"),
            "taxa_adm_aa": st.column_config.NumberColumn("Taxa adm (% a.a.)", format="%.2f"),
            "isento_ir": st.column_config.CheckboxColumn("Isento IR"),
            "come_cotas": st.column_config.CheckboxColumn("Come-cotas"),
        },
    )

    atualizar_existentes = st.checkbox(
        "Atualizar ativos já cadastrados (mesmo titular e nome): grava snapshot e CNPJ",
        value=True,
    )

    if st.button("💾 Gravar selecionados", type="primary"):
        titular_id = mapa[titular_nome]
        existentes = {
            a["nome"].strip().lower(): int(a["id"])
            for a in database.listar_ativos(titular_id=titular_id, apenas_ativos=False)
        }
        criados = atualizados = ignorados = 0
        total_gravado = 0.0
        for _, linha in editado.iterrows():
            if not bool(linha["incluir"]):
                continue
            nome = str(linha["nome"]).strip()
            if not nome:
                continue
            cnpj_digitos = "".join(ch for ch in str(linha["cnpj"] or "") if ch.isdigit()) or None
            saldo = float(linha["saldo_bruto"])
            data_snap = str(linha["data_aplicacao"]).strip() or (meta.data_referencia or date.today().isoformat())

            existente_id = existentes.get(nome.lower())
            if existente_id is not None:
                if not atualizar_existentes:
                    ignorados += 1
                    continue
                campos = {"cnpj": cnpj_digitos, "categoria": str(linha["categoria"])}
                database.atualizar_ativo(existente_id, campos)
                database.registrar_snapshot(existente_id, data_snap, saldo)
                atualizados += 1
            else:
                novo_id = database.inserir_ativo(
                    titular_id=titular_id,
                    nome=nome,
                    categoria=str(linha["categoria"]),
                    taxa_adm_aa=float(linha["taxa_adm_aa"]) or None,
                    isento_ir=bool(linha["isento_ir"]),
                    come_cotas=bool(linha["come_cotas"]),
                    data_aplicacao=data_snap,
                    valor_aplicado=float(linha["valor_aplicado"]),
                    cnpj=cnpj_digitos,
                    observacoes=f"Importado do extrato XP (conta {meta.conta or '?'}, ref. {meta.data_referencia or '?'})",
                )
                database.registrar_snapshot(novo_id, data_snap, saldo)
                criados += 1
            total_gravado += saldo

        st.success(
            f"Gravação concluída: {criados} novos, {atualizados} atualizados, "
            f"{ignorados} ignorados. Total: {brl(round(total_gravado, 2))}."
        )
        st.session_state.pop("import_estado", None)


# --------------------------------------------------------------------------- #
# Roteador principal
# --------------------------------------------------------------------------- #
def main() -> None:
    titular_id = render_sidebar()
    abas = st.tabs(
        [
            "📊 Painel",
            "📋 Ativos",
            "📥 Importar extrato",
            "📝 Atualizar valores",
            "🎯 Metas e projeções",
            "🧠 Consultor IA",
            "🎮 Simulador",
            "💰 Proventos & IR",
        ]
    )
    with abas[0]:
        aba_painel(titular_id)
    with abas[1]:
        aba_ativos(titular_id)
    with abas[2]:
        aba_importar()
    with abas[3]:
        aba_atualizar(titular_id)
    with abas[4]:
        aba_metas(titular_id)
    with abas[5]:
        aba_consultor(titular_id)
    with abas[6]:
        aba_simulador()
    with abas[7]:
        aba_proventos_ir(titular_id)


if __name__ == "__main__":
    main()
