import streamlit as st
import pandas as pd
import xml.etree.ElementTree as ET
from pypdf import PdfReader
import zipfile
import io
import re
from concurrent.futures import ThreadPoolExecutor
import os

st.set_page_config(page_title="Extrator de Notas Fiscais", layout="wide")

# --- FUNÇÕES DE EXTRAÇÃO (REGEX E XML) ---

def extrair_dados_xml(conteudo_bytes):
    """Extrai dados de um XML de NF-e/NFS-e padrão."""
    try:
        # Remove namespaces para facilitar a busca por tags
        xml_str = conteudo_bytes.decode('utf-8', errors='ignore')
        xml_str = re.sub(r'xmlns="[^"]"', '', xml_str)
        root = ET.fromstring(xml_str)
        
        # Como o XML pode variar por município/estado, usamos caminhos flexíveis (.//)
        def buscar_tag(tags):
            for tag in tags:
                el = root.find(f".//{tag}")
                if el is not None and el.text:
                    return el.text.strip()
            return ""

        numero = buscar_tag(['nNF', 'Numero', 'numeroNota'])
        cnpj = buscar_tag(['CNPJ', 'Cnpj', 'cnpjPrestador', 'cnpjEmitente'])
        razao_social = buscar_tag(['xNome', 'RazaoSocial', 'nomePrestador', 'nomeEmitente'])
        valor = buscar_tag(['vNF', 'ValorServicos', 'valorLiquido', 'vProd'])
        data = buscar_tag(['dhEmi', 'DataEmissao', 'dtEmissao', 'dEmi'])
        
        # Limpeza simples da data (pega apenas AAAA-MM-DD se houver hora)
        if data and len(data) >= 10:
            data = data[:10]

        return {
            "Número da Nota": numero,
            "Razão Social": razao_social,
            "CNPJ": cnpj,
            "Valor": valor,
            "Data": data,
            "Fornecedor": razao_social # Geralmente o emitente é o fornecedor
        }
    except Exception:
        return None

def extrair_dados_pdf(conteudo_bytes):
    """Extrai dados de um PDF usando Expressões Regulares (Regex)."""
    try:
        pdf = PdfReader(io.BytesIO(conteudo_bytes))
        texto = ""
        for page in pdf.pages:
            texto += page.extract_text() or ""
            
        if not texto:
            return None

        # Padrões Regex (Podem precisar de ajustes dependendo do layout do seu fornecedor)
        # Nota: PDFs não têm padrão fixo como XML, estes regex pegam formatos comuns
        numero_match = re.search(r'(?:NÚMERO|NUMERO|Nº|Nota Nº)\s*[:.]?\s*(\d+)', texto, re.IGNORECASE)
        cnpj_match = re.search(r'(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}|\d{14})', texto)
        valor_match = re.search(r'(?:VALOR TOTAL|VALOR LIQUIDO|TOTAL DA NOTA|R\$)\s*[:.]?\s*([\d.,]+)', texto, re.IGNORECASE)
        data_match = re.search(r'(\d{2}/\d{2}/\d{4})', texto)
        
        # Tentativa de pegar Razão Social (geralmente próxima ao CNPJ ou no topo)
        razao_match = re.search(r'(?:Razão Social|Razao Social|Prestador|Emitente)\s*[:.]?\s*([A-Za-z0-9 ]+)', texto, re.IGNORECASE)

        numero = numero_match.group(1) if numero_match else ""
        cnpj = cnpj_match.group(1) if cnpj_match else ""
        valor = valor_match.group(1) if valor_match else ""
        data = data_match.group(1) if data_match else ""
        razao_social = razao_match.group(1).strip() if razao_match else "Verificar no PDF"

        return {
            "Número da Nota": numero,
            "Razão Social": razao_social,
            "CNPJ": cnpj,
            "Valor": valor,
            "Data": data,
            "Fornecedor": razao_social
        }
    except Exception:
        return None

# --- PROCESSADOR INDIVIDUAL ---

def processar_arquivo(item):
    """Processa um único arquivo com base na extensão."""
    nome_arquivo, conteudo = item
    nome_lower = nome_arquivo.lower()
    
    dados = None
    if nome_lower.endswith('.xml'):
        dados = extrair_dados_xml(conteudo)
    elif nome_lower.endswith('.pdf'):
        dados = extrair_dados_pdf(conteudo)
        
    if dados:
        dados["Arquivo"] = os.path.basename(nome_arquivo)
        return dados
    return None

# --- INTERFACE DO STREAMLIT ---

st.title("⚡ Extrator Ultrarápido de Notas Fiscais (XML / PDF)")
st.write("Anexe arquivos soltos ou um arquivo **.ZIP** contendo até milhares de notas fiscais.")

# Aceita múltiplos arquivos ou um arquivo ZIP
arquivos_carregados = st.file_uploader(
    "Escolha os arquivos XML/PDF ou um arquivo ZIP", 
    type=["xml", "pdf", "zip"], 
    allow_output_mutation=False, 
    accept_multiple_files=True
)

if arquivos_carregados:
    lista_arquivos = []
    
    # 1. Desempacota e lê os arquivos na memória
    with st.spinner("Lendo arquivos enviados..."):
        for arq in arquivos_carregados:
            if arq.name.lower().endswith('.zip'):
                try:
                    with zipfile.ZipFile(io.BytesIO(arq.read())) as z:
                        for nome_interno in z.namelist():
                            if nome_interno.lower().endswith(('.xml', '.pdf')):
                                lista_arquivos.append((nome_interno, z.read(nome_interno)))
                except Exception as e:
                    st.error(f"Erro ao ler o arquivo ZIP {arq.name}: {e}")
            else:
                lista_arquivos.append((arq.name, arq.read()))

    total_arquivos = len(lista_arquivos)
    
    if total_arquivos > 0:
        st.info(f"Total de {total_arquivos} notas fiscais detectadas. Iniciando processamento...")
        
        # Progress bar
        barra_progresso = st.progress(0)
        
        # 2. Processamento Paralelo utilizando Threads para alta velocidade (I/O bound)
        resultados = []
        # O Streamlit Cloud tem limites de CPU, usar min(32, total) aproveita o máximo sem estourar a máquina
        max_workers = min(32, os.cpu_count() * 4 or 16) 
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Mapeia a função para a lista de arquivos
            for i, resultado in enumerate(executor.map(processar_arquivo, lista_arquivos)):
                if resultado:
                    resultados.append(resultado)
                # Atualiza a barra de progresso dinamicamente
                barra_progresso.progress((i + 1) / total_arquivos)

        # 3. Exibição dos resultados
        if resultados:
            df = pd.DataFrame(resultados)
            
            # Reorganiza colunas
            colunas_ordem = ["Número da Nota", "Razão Social", "CNPJ", "Valor", "Data", "Fornecedor", "Arquivo"]
            df = df[colunas_ordem]
            
            st.success(f"Sucesso! {len(resultados)} notas processadas com êxito.")
            
            # Pré-visualização da Tabela
            st.subheader("📋 Pré-visualização dos Dados")
            st.dataframe(df, use_container_width=True)
            
            # 4. Geração do arquivo Excel na memória para Download
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
            st.warning("Nenhum dado pôde ser extraído dos arquivos enviados. Verifique o formato dos PDFs/XMLs.")
    else:
        st.warning("Nenhum arquivo válido (.xml ou .pdf) foi encontrado.")