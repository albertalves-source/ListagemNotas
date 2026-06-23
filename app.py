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

st.set_page_config(page_title="Extrator de Notas Fiscais (IA + Memória)", layout="wide")

# --- MEMÓRIA DO APLICATIVO (Evita perder dados se a tela piscar) ---
if "resultados_salvos" not in st.session_state:
    st.session_state.resultados_salvos = []
if "arquivos_processados" not in st.session_state:
    st.session_state.arquivos_processados = set()

# --- CONFIGURAÇÃO DA IA ---
CHAVE_API = st.secrets.get("GEMINI_API_KEY", "")

if CHAVE_API:
    genai.configure(api_key=CHAVE_API)
    modelo_ia = genai.GenerativeModel('gemini-2.5-flash')

# --- FUNÇÃO DA IA ---
def extrair_dados_com_ia(conteudo_extra):
    prompt = """
    Você é um assistente especialista em contabilidade. Analise o conteúdo fornecido desta nota fiscal e extraia os dados abaixo.
    Retorne ESTRITAMENTE um JSON válido com estas chaves exatas (e nada mais):
    {"Número da Nota": "", "CNPJ": "", "Valor": "", "Data": "", "Fornecedor": ""}

    Regras:
    - Fornecedor: É o Prestador de Serviços ou Emitente. Ignore nomes de prefeituras, municípios ou do tomador.
    - CNPJ: O CNPJ do Prestador/Emitente.
    - Data: Formato DD/MM/AAAA.
    - Valor: O valor total ou líquido do serviço (apenas o número e a vírgula).
    Se não encontrar a informação, deixe a string vazia "".
    """
    
    tentativas = 4
    for tentativa in range(tentativas):
        try:
            resposta = modelo_ia.generate_content(
                [prompt, conteudo_extra],
                generation_config=genai.GenerationConfig(response_mime_type="application/json")
            )
            
            texto_resposta = resposta.text.strip()
            if texto_resposta.startswith("```"):
                match = re.search(r'\{.*\}', texto_resposta, re.DOTALL)
                if match:
                    texto_resposta = match.group(0)
                    
            return json.loads(texto_resposta)
            
        except Exception as e:
            erro_str = str(e).lower()
            if "429" in erro_str or "exhausted" in erro_str or "quota" in erro_str:
                if tentativa < tentativas - 1:
                    time.sleep(15) 
                    continue
            return {"Número da Nota": "Erro", "CNPJ": "Erro", "Valor": "Erro", "Data": "Erro", "Fornecedor": f"Falha IA: {str(e)[:40]}"}
            
    return {"Número da Nota": "Erro", "CNPJ": "Erro", "Valor": "Erro", "Data": "Erro", "Fornecedor": "Limite de API excedido"}

# --- EXTRAÇÃO DE ARQUIVOS ---
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
    except Exception as e:
        dados["Fornecedor"] = f"Erro no XML: {str(e)[:30]}"
    return dados

def processar_arquivo(item):
    nome_arquivo, conteudo = item
    nome_lower = nome_arquivo.lower()
    
    dados = None
    if nome_lower.endswith('.xml'):
        dados = extrair_dados_xml(conteudo)
    elif nome_lower.endswith('.pdf'):
        texto_pdf = ""
        try:
            with pdfplumber.open(io.BytesIO(conteudo)) as pdf:
                for pagina in pdf.pages:
                    t = pagina.extract_text()
                    if t: texto_pdf += t + "\n"
        except Exception:
            pass
            
        if len(texto_pdf.strip()) > 50:
            dados = extrair_dados_com_ia(texto_pdf)
        else:
            try:
                imagens = convert_from_bytes(conteudo, dpi=150)
                if imagens:
                    dados = extrair_dados_com_ia(imagens[0])
                else:
                    dados = {"Número da Nota": "", "CNPJ": "", "Valor": "", "Data": "", "Fornecedor": "PDF Vazio"}
            except Exception:
                dados = {"Número da Nota": "", "CNPJ": "", "Valor": "", "Data": "", "Fornecedor": "Erro de Leitura de Imagem"}
                
        time.sleep(4.5)
        
    if not dados:
        dados = {"Número da Nota": "", "CNPJ": "", "Valor": "", "Data": "", "Fornecedor": "Formato desconhecido"}
        
    dados["Arquivo"] = os.path.basename(nome_arquivo)
    
    chaves_corretas = ["Número da Nota", "CNPJ", "Valor", "Data", "Fornecedor", "Arquivo"]
    return {k: dados.get(k, "") for k in chaves_corretas}

# --- INTERFACE DO STREAMLIT ---
st.title("🧠 Extrator de Notas Fiscais Inteligente")
st.warning("⚠️ **Dica Importante:** Para evitar que o servidor reinicie, envie lotes de no máximo **50 a 100 notas por vez**.")

if not CHAVE_API:
    st.error("⚠️ Atenção: A chave da API do Gemini não foi detectada.")

# Botão para limpar a memória caso o usuário queira começar um lote novo do zero
if st.sidebar.button("🗑️ Limpar Memória e Começar de Novo"):
    st.session_state.resultados_salvos = []
    st.session_state.arquivos_processados = set()
    st.rerun()

st.sidebar.info(f"Notas na Memória Atual: **{len(st.session_state.resultados_salvos)}**")

arquivos_carregados = st.file_uploader(
    "Escolha os arquivos XML/PDF ou um arquivo ZIP", 
    type=["xml", "pdf", "zip"], 
    accept_multiple_files=True
)

if arquivos_carregados and CHAVE_API:
    lista_arquivos = []
    
    with st.spinner("Desempacotando arquivos enviados..."):
        for arq in arquivos_carregados:
            if arq.name.lower().endswith('.zip'):
                try:
                    zip_data = arq.read()
                    with zipfile.ZipFile(io.BytesIO(zip_data)) as z:
                        for nome_interno in z.namelist():
                            if nome_interno.lower().endswith(('.xml', '.pdf')):
                                if not nome_interno.startswith('__MACOSX') and not os.path.basename(nome_interno).startswith('.'):
                                    lista_arquivos.append((nome_interno, z.read(nome_interno)))
                except Exception:
                    pass
            else:
                lista_arquivos.append((arq.name, arq.read()))

    total_arquivos = len(lista_arquivos)
    
    if total_arquivos > 0:
        arquivos_pendentes = [arq for arq in lista_arquivos if arq[0] not in st.session_state.arquivos_processados]
        
        if arquivos_pendentes:
            st.info(f"Processando {len(arquivos_pendentes)} novas notas...")
            barra_progresso = st.progress(0)
            
            # Espaço reservado para mostrar a tabela crescendo ao vivo
            tabela_placeholder = st.empty()
            
            for i, item in enumerate(arquivos_pendentes):
                resultado = processar_arquivo(item)
                
                # Salva na memória instantaneamente
                st.session_state.resultados_salvos.append(resultado)
                st.session_state.arquivos_processados.add(item[0])
                
                # Atualiza a tela a cada nota processada
                df_parcial = pd.DataFrame(st.session_state.resultados_salvos)
                colunas_ordem = ["Número da Nota", "CNPJ", "Valor", "Data", "Fornecedor", "Arquivo"]
                df_parcial = df_parcial[colunas_ordem]
                tabela_placeholder.dataframe(df_parcial, use_container_width=True)
                
                barra_progresso.progress((i + 1) / len(arquivos_pendentes))
                
            st.success("Leitura concluída!")
        else:
            st.success("Todas as notas deste lote já estão na memória.")
            
        # Exibe o botão de Download com base no que está na memória
        if st.session_state.resultados_salvos:
            df_final = pd.DataFrame(st.session_state.resultados_salvos)
            colunas_ordem = ["Número da Nota", "CNPJ", "Valor", "Data", "Fornecedor", "Arquivo"]
            df_final = df_final[colunas_ordem]
            
            if not arquivos_pendentes:
                st.subheader("📋 Dados Prontos")
                st.dataframe(df_final, use_container_width=True)
            
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df_final.to_excel(writer, index=False, sheet_name='Notas Fiscais')
            dados_excel = output.getvalue()
            
            st.download_button(
                label="📥 Baixar Tabela em Excel",
                data=dados_excel,
                file_name="Notas_Fiscais_Processadas_IA.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
