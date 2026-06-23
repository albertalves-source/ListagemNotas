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

st.set_page_config(page_title="Extrator de Notas Fiscais (IA Robusta)", layout="wide")

# --- CONFIGURAÇÃO DA IA ---
CHAVE_API = st.secrets.get("GEMINI_API_KEY", "")

if CHAVE_API:
    genai.configure(api_key=CHAVE_API)
    modelo_ia = genai.GenerativeModel('gemini-2.5-flash')

# --- FUNÇÃO DA INTELIGÊNCIA ARTIFICIAL COM AUTO-RETRY ---
def extrair_dados_com_ia(conteudo_extra):
    """Envia o texto ou a IMAGEM da nota para a IA analisar, com sistema anti-bloqueio."""
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
            # Se o erro for de limite de API (429), espera 15s e tenta novamente
            if "429" in erro_str or "exhausted" in erro_str or "quota" in erro_str:
                if tentativa < tentativas - 1:
                    time.sleep(15) 
                    continue
            
            # Se for outro erro ou acabaram as tentativas
            return {"Número da Nota": "Erro", "CNPJ": "Erro", "Valor": "Erro", "Data": "Erro", "Fornecedor": f"Falha IA: {str(e)[:40]}"}
            
    return {"Número da Nota": "Erro", "CNPJ": "Erro", "Valor": "Erro", "Data": "Erro", "Fornecedor": "Limite de API excedido após retentativas"}

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
            
        # Se tem texto normal, manda o texto pra IA
        if len(texto_pdf.strip()) > 50:
            dados = extrair_dados_com_ia(texto_pdf)
        else:
            # SE FOR IMAGEM ESCANEADA: Tira "foto" do PDF e manda pra IA Visão
            try:
                imagens = convert_from_bytes(conteudo, dpi=150)
                if imagens:
                    dados = extrair_dados_com_ia(imagens[0])
                else:
                    dados = {"Número da Nota": "", "CNPJ": "", "Valor": "", "Data": "", "Fornecedor": "PDF Vazio/Corrompido"}
            except Exception as e:
                dados = {"Número da Nota": "", "CNPJ": "", "Valor": "", "Data": "", "Fornecedor": "Erro de OCR (Instale o poppler-utils)"}
                
        # Pausa padrão obrigatória
        time.sleep(4.5)
        
    if not dados:
        dados = {"Número da Nota": "", "CNPJ": "", "Valor": "", "Data": "", "Fornecedor": "Formato desconhecido"}
        
    dados["Arquivo"] = os.path.basename(nome_arquivo)
    
    chaves_corretas = ["Número da Nota", "CNPJ", "Valor", "Data", "Fornecedor", "Arquivo"]
    dados_finais = {k: dados.get(k, "") for k in chaves_corretas}
    return dados_finais

# --- INTERFACE DO STREAMLIT ---
st.title("🧠 Extrator de Notas Fiscais Inteligente (Visão + Anti-Bloqueio)")
st.write("A IA lê texto, analisa PDFs escaneados como imagem e lida automaticamente com limites da API.")

if not CHAVE_API:
    st.error("⚠️ Atenção: A chave da API do Gemini não foi detectada. Verifique os Secrets do Streamlit Cloud.")

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
                    st.error(f"Erro ao ler ZIP: {e}")
            else:
                lista_arquivos.append((arq.name, arq.read()))

    total_arquivos = len(lista_arquivos)
    
    if total_arquivos > 0:
        tempo_estimado_min = (total_arquivos * 5) / 60
        st.info(f"Processando {total_arquivos} notas. Se a API atingir o limite, o sistema aguardará 15s automaticamente. Tempo estimado: ~{tempo_estimado_min:.1f} minutos.")
        
        barra_progresso = st.progress(0)
        resultados = []
        
        for i, item in enumerate(lista_arquivos):
            resultado = processar_arquivo(item)
            resultados.append(resultado)
            barra_progresso.progress((i + 1) / total_arquivos)

        if resultados:
            df = pd.DataFrame(resultados)
            
            st.success("Leitura concluída!")
            
            st.subheader("📋 Pré-visualização dos Dados")
            st.dataframe(df, use_container_width=True)
            
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df.to_excel(writer, index=False, sheet_name='Notas Fiscais')
            dados_excel = output.getvalue()
            
            st.download_button(
                label="📥 Baixar Tabela em Excel",
                data=dados_excel,
                file_name="Notas_Fiscais_Processadas_IA.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
