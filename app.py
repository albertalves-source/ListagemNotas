import streamlit as st
import pandas as pd
import xml.etree.ElementTree as ET
import pdfplumber
import zipfile
import io
import os
import json
import time
import re
import google.generativeai as genai
from pdf2image import convert_from_bytes

st.set_page_config(page_title="Extrator de Notas Fiscais (Plano Free)", layout="wide")

# --- MEMÓRIA E CONTROLE DE CHAVES ---
if "resultados_salvos" not in st.session_state:
    st.session_state.resultados_salvos = []
if "arquivos_processados" not in st.session_state:
    st.session_state.arquivos_processados = set()
if "indice_chave_atual" not in st.session_state:
    st.session_state.indice_chave_atual = 0

CHAVES_API = st.secrets.get("GEMINI_API_KEYS", [])

def configurar_ia():
    """Configura a IA com a chave atual da lista."""
    if not CHAVES_API or st.session_state.indice_chave_atual >= len(CHAVES_API):
        return None
    genai.configure(api_key=CHAVES_API[st.session_state.indice_chave_atual])
    return genai.GenerativeModel('gemini-2.5-flash')

# --- FUNÇÃO DA IA COM ROTAÇÃO AUTOMÁTICA ---
def extrair_dados_com_ia(conteudo_extra):
    prompt = """
    Você é um assistente especialista em contabilidade. Analise esta nota fiscal e extraia os dados abaixo.
    Retorne ESTRITAMENTE um JSON válido com estas chaves exatas (e nada mais):
    {"Número da Nota": "", "CNPJ": "", "Valor": "", "Data": "", "Fornecedor": ""}

    Regras:
    - Fornecedor: É o Prestador de Serviços ou Emitente. Ignore clientes e prefeituras.
    - CNPJ: O CNPJ do Prestador/Emitente.
    - Data: Formato DD/MM/AAAA.
    - Valor: O valor total ou líquido.
    Se não encontrar a informação, deixe a string vazia "".
    """
    
    while st.session_state.indice_chave_atual < len(CHAVES_API):
        modelo = configurar_ia()
        if not modelo: break
            
        try:
            resposta = modelo.generate_content(
                [prompt, conteudo_extra],
                generation_config=genai.GenerationConfig(response_mime_type="application/json")
            )
            
            texto = resposta.text.strip()
            if texto.startswith("```"):
                match = re.search(r'\{.*\}', texto, re.DOTALL)
                if match: texto = match.group(0)
            return json.loads(texto)
            
        except Exception as e:
            erro = str(e).lower()
            # Se for o erro 429 (Cota de 20 estourada), pula para a próxima chave!
            if "429" in erro or "quota" in erro or "exhausted" in erro:
                st.session_state.indice_chave_atual += 1
                time.sleep(3) # Pausa rápida para o sistema respirar antes da nova chave
                continue
            return {"Número da Nota": "Erro", "CNPJ": "Erro", "Valor": "Erro", "Data": "Erro", "Fornecedor": f"Falha IA: {str(e)[:40]}"}
            
    return {"Número da Nota": "Erro", "CNPJ": "Erro", "Valor": "Erro", "Data": "Erro", "Fornecedor": "🚨 FIM DO LIMITE: Todas as chaves foram usadas hoje."}

# --- PROCESSAMENTO DE ARQUIVOS ---
def extrair_dados_xml(conteudo_bytes):
    dados = {"Número da Nota": "", "CNPJ": "", "Valor": "", "Data": "", "Fornecedor": ""}
    try:
        xml_str = conteudo_bytes.decode('utf-8', errors='ignore')
        xml_str = re.sub(r'xmlns="[^"]*"', '', xml_str) 
        root = ET.fromstring(xml_str)
        def buscar_tag(tags, parent=root):
            if parent is None: return ""
            for tag in tags:
                el = parent.find(f".//{tag}")
                if el is not None and el.text: return el.text.strip()
            return ""
        numero = buscar_tag(['nNF', 'Numero', 'numeroNota', 'nNFse'])
        if numero: dados["Número da Nota"] = numero.lstrip('0')
        prestador = root.find('.//Prestador') or root.find('.//emit') or root.find('.//PrestadorServico')
        if prestador is not None:
            dados["CNPJ"] = buscar_tag(['CNPJ', 'Cnpj'], prestador)
            dados["Fornecedor"] = buscar_tag(['xFant', 'nomeFantasia', 'xNome', 'RazaoSocial'], prestador)
        else:
            dados["CNPJ"] = buscar_tag(['CNPJ', 'Cnpj', 'cnpjPrestador'])
            dados["Fornecedor"] = buscar_tag(['xFant', 'nomeFantasia', 'xNome', 'RazaoSocial'])
        dados["Valor"] = buscar_tag(['vNF', 'ValorServicos', 'valorLiquido', 'vProd', 'vLiq'])
        data = buscar_tag(['dhEmi', 'DataEmissao', 'dtEmissao', 'dEmi'])
        if data and len(data) >= 10: dados["Data"] = data[:10]
    except Exception: pass
    return dados

def processar_arquivo(item):
    nome_arquivo, conteudo = item
    dados = None
    if nome_arquivo.lower().endswith('.xml'):
        dados = extrair_dados_xml(conteudo)
    elif nome_arquivo.lower().endswith('.pdf'):
        texto_pdf = ""
        try:
            with pdfplumber.open(io.BytesIO(conteudo)) as pdf:
                # TRAVA: Só lê a primeira página para poupar o limite grátis!
                t = pdf.pages[0].extract_text()
                if t: texto_pdf += t + "\n"
        except Exception: pass
            
        if len(texto_pdf.strip()) > 50:
            dados = extrair_dados_com_ia(texto_pdf)
        else:
            try:
                # TRAVA: Transforma apenas a página 1 em imagem
                imagens = convert_from_bytes(conteudo, dpi=150, first_page=1, last_page=1)
                if imagens: dados = extrair_dados_com_ia(imagens[0])
            except Exception: pass
                
        # Pausa de 5 segundos para não estourar o limite por minuto
        time.sleep(5)
        
    if not dados: dados = {"Número da Nota": "", "CNPJ": "", "Valor": "", "Data": "", "Fornecedor": "Inválido ou Corrompido"}
    dados["Arquivo"] = os.path.basename(nome_arquivo)
    chaves = ["Número da Nota", "CNPJ", "Valor", "Data", "Fornecedor", "Arquivo"]
    return {k: dados.get(k, "") for k in chaves}

# --- INTERFACE ---
st.title("🤖 Extrator de Notas Fiscais (Free Auto-Switch)")
st.write("Coloque o seu ZIP. O sistema trocará de conta automaticamente quando o limite de 20 for atingido.")

if not CHAVES_API:
    st.error("⚠️ Defina a lista GEMINI_API_KEYS nos Secrets do Streamlit.")
else:
    chaves_restantes = len(CHAVES_API) - st.session_state.indice_chave_atual
    st.sidebar.success(f"🔑 Chaves com Saldo: {chaves_restantes} de {len(CHAVES_API)}")

if st.sidebar.button("🗑️ Limpar Memória e Resetar Chaves"):
    st.session_state.resultados_salvos = []
    st.session_state.arquivos_processados = set()
    st.session_state.indice_chave_atual = 0
    st.rerun()

st.sidebar.info(f"Notas Lidas com Sucesso: **{len(st.session_state.resultados_salvos)}**")

arquivos_carregados = st.file_uploader("ZIP ou Arquivos Soltos", type=["xml", "pdf", "zip"], accept_multiple_files=True)

if arquivos_carregados and CHAVES_API:
    lista_arquivos = []
    with st.spinner("Preparando arquivos..."):
        for arq in arquivos_carregados:
            if arq.name.lower().endswith('.zip'):
                try:
                    with zipfile.ZipFile(io.BytesIO(arq.read())) as z:
                        for nome in z.namelist():
                            if nome.lower().endswith(('.xml', '.pdf')) and not nome.startswith('__'):
                                lista_arquivos.append((nome, z.read(nome)))
                except Exception: pass
            else:
                lista_arquivos.append((arq.name, arq.read()))

    arquivos_pendentes = [a for a in lista_arquivos if a[0] not in st.session_state.arquivos_processados]
    
    if arquivos_pendentes:
        st.info(f"Processando {len(arquivos_pendentes)} notas ao vivo...")
        barra = st.progress(0)
        tabela_placeholder = st.empty()
        
        for i, item in enumerate(arquivos_pendentes):
            if st.session_state.indice_chave_atual >= len(CHAVES_API):
                st.error("🛑 Todas as chaves atingiram o limite de hoje. Volte amanhã ou adicione mais chaves.")
                break
                
            res = processar_arquivo(item)
            st.session_state.resultados_salvos.append(res)
            st.session_state.arquivos_processados.add(item[0])
            
            df_parcial = pd.DataFrame(st.session_state.resultados_salvos)[["Número da Nota", "CNPJ", "Valor", "Data", "Fornecedor", "Arquivo"]]
            tabela_placeholder.dataframe(df_parcial, use_container_width=True)
            barra.progress((i + 1) / len(arquivos_pendentes))
            
    if st.session_state.resultados_salvos:
        df_final = pd.DataFrame(st.session_state.resultados_salvos)[["Número da Nota", "CNPJ", "Valor", "Data", "Fornecedor", "Arquivo"]]
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer: df_final.to_excel(writer, index=False)
        st.download_button("📥 Baixar Tabela em Excel", output.getvalue(), "Notas_Fiscais_Processadas.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
