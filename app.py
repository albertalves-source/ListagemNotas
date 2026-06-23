import streamlit as st
import pandas as pd
import xml.etree.ElementTree as ET
import pdfplumber
import zipfile
import io
import os
import json
import google.generativeai as genai
import time

st.set_page_config(page_title="Extrator de Notas Fiscais (IA Inteligente)", layout="wide")

# --- CONFIGURAÇÃO DA IA ---
# A chave de API deve ser configurada nos Secrets do Streamlit Cloud
CHAVE_API = st.secrets.get("GEMINI_API_KEY", "")

if CHAVE_API:
    genai.configure(api_key=CHAVE_API)
    # Usamos o modelo flash por ser rápido e excelente em extração de dados
    modelo_ia = genai.GenerativeModel('gemini-1.5-flash')

# --- FUNÇÃO DA INTELIGÊNCIA ARTIFICIAL ---
def extrair_dados_com_ia(texto_nota):
    """Envia o texto da nota para a IA analisar o contexto e extrair os dados."""
    prompt = f"""
    Você é um assistente especialista em contabilidade e notas fiscais brasileiras (NFS-e e NF-e).
    Vou te passar o texto extraído de um PDF de nota fiscal. O texto pode estar bagunçado.
    Leia com calma, analise o contexto e encontre exatamente os seguintes dados:
    1. Número da Nota
    2. CNPJ do Fornecedor (Prestador do serviço ou Emitente. Ignore o CNPJ do tomador/cliente ou da prefeitura).
    3. Nome do Fornecedor (Razão Social ou Nome Fantasia do Prestador/Emitente. Não confunda com órgãos públicos ou com o cliente).
    4. Valor Total ou Líquido da nota.
    5. Data de Emissão.

    Retorne APENAS um objeto JSON válido e nada mais, com estas chaves exatas:
    {{"Número da Nota": "", "CNPJ": "", "Valor": "", "Data": "", "Fornecedor": ""}}
    Se não encontrar algum dado com certeza, deixe a string vazia "".

    Texto da Nota Fiscal:
    {texto_nota}
    """
    
    try:
        resposta = modelo_ia.generate_content(prompt)
        texto_resposta = resposta.text.strip()
        
        # Limpa formatações markdown caso a IA retorne com elas
        if texto_resposta.startswith("```json"):
            texto_resposta = texto_resposta[7:-3]
        elif texto_resposta.startswith("```"):
            texto_resposta = texto_resposta[3:-3]
            
        dados_json = json.loads(texto_resposta.strip())
        return dados_json
    except Exception as e:
        return {"Número da Nota": "", "CNPJ": "", "Valor": "", "Data": "", "Fornecedor": ""}

# --- EXTRAÇÃO DE ARQUIVOS ---
def extrair_dados_xml(conteudo_bytes):
    """Para XML, a extração via código é 100% segura, não precisa de IA."""
    dados = {"Número da Nota": "", "CNPJ": "", "Valor": "", "Data": "", "Fornecedor": ""}
    try:
        xml_str = conteudo_bytes.decode('utf-8', errors='ignore')
        import re
        xml_str = re.sub(r'xmlns="[^"]*"', '', xml_str) 
        root = ET.fromstring(xml_str)
        
        def buscar_tag(tags, parent=root):
            if parent is None: return ""
            for tag in tags:
                el = parent.find(f".//{tag}")
                if el is not None and el.text:
                    return el.text.strip()
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
        if data and len(data) >= 10:
            dados["Data"] = data[:10]
    except Exception:
        pass
    return dados

def processar_arquivo(item):
    """Lê o arquivo um por um, com calma."""
    nome_arquivo, conteudo = item
    nome_lower = nome_arquivo.lower()
    
    dados = None
    if nome_lower.endswith('.xml'):
        dados = extrair_dados_xml(conteudo)
    elif nome_lower.endswith('.pdf'):
        # Extrai o texto base
        texto_pdf = ""
        try:
            with pdfplumber.open(io.BytesIO(conteudo)) as pdf:
                for pagina in pdf.pages:
                    t = pagina.extract_text()
                    if t: texto_pdf += t + "\n"
        except Exception:
            pass
            
        if texto_pdf.strip():
            # Passa para a IA ler com contexto humano
            dados = extrair_dados_com_ia(texto_pdf)
            # Pausa de 1 segundo para não sobrecarregar a API gratuita
            time.sleep(1)
        
    if not dados:
        dados = {"Número da Nota": "", "CNPJ": "", "Valor": "", "Data": "", "Fornecedor": ""}
        
    dados["Arquivo"] = os.path.basename(nome_arquivo)
    return dados

# --- INTERFACE DO STREAMLIT ---
st.title("🧠 Extrator de Notas Fiscais Inteligente (Com IA)")
st.write("Este sistema usa Inteligência Artificial para ler cada PDF e interpretar os dados de fornecedores, ignorando formatos confusos de prefeituras.")

if not CHAVE_API:
    st.error("⚠️ Falta configurar a chave da API do Gemini. Vá nos Secrets do Streamlit e adicione `GEMINI_API_KEY = 'sua_chave'`.")

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
                except Exception as e:
                    st.error(f"Erro ao ler o arquivo ZIP {arq.name}: {e}")
            else:
                lista_arquivos.append((arq.name, arq.read()))

    total_arquivos = len(lista_arquivos)
    
    if total_arquivos > 0:
        st.info(f"Total de {total_arquivos} notas detectadas. A IA fará a leitura individual detalhada (isso pode levar alguns minutos)...")
        
        barra_progresso = st.progress(0)
        resultados = []
        
        # Leitura sequencial "com calma", uma por vez, para alta precisão e sem estourar limites de API
        for i, item in enumerate(lista_arquivos):
            resultado = processar_arquivo(item)
            resultados.append(resultado)
            barra_progresso.progress((i + 1) / total_arquivos)

        if resultados:
            df = pd.DataFrame(resultados)
            
            colunas_ordem = ["Número da Nota", "CNPJ", "Valor", "Data", "Fornecedor", "Arquivo"]
            for col in colunas_ordem:
                if col not in df.columns:
                    df[col] = ""
            df = df[colunas_ordem]
            
            st.success(f"Leitura concluída com precisão para {total_arquivos} notas.")
            
            st.subheader("📋 Pré-visualização dos Dados")
            st.dataframe(df, use_container_width=True)
            
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df.to_excel(writer, index=False, sheet_name='Notas Fiscais')
            dados_excel = output.getvalue()
            
            st.download_button(
                label="📥 Baixar Tabela em Excel",
                data=dados_excel,
                file_name="Notas_Fiscais_Processadas.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
        else:
            st.warning("Nenhum dado pôde ser processado.")
