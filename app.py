import streamlit as st
import pandas as pd
import xml.etree.ElementTree as ET
import pdfplumber
import zipfile
import io
import re
from concurrent.futures import ThreadPoolExecutor
import os

st.set_page_config(page_title="Extrator de Notas Fiscais", layout="wide")

# --- FUNÇÕES DE EXTRAÇÃO PURA (SEM TEXTOS GENÉRICOS) ---

def extrair_dados_xml(conteudo_bytes):
    """Extração cirúrgica de XML de NF-e/NFS-e."""
    dados = {"Número da Nota": "", "CNPJ": "", "Valor": "", "Data": "", "Fornecedor": ""}
    try:
        xml_str = conteudo_bytes.decode('utf-8', errors='ignore')
        # Remove namespaces para leitura limpa das tags
        xml_str = re.sub(r'xmlns="[^"]*"', '', xml_str)
        root = ET.fromstring(xml_str)
        
        def buscar_tag(tags, elemento_pai=root):
            for tag in tags:
                el = elemento_pai.find(f".//{tag}")
                if el is not None and el.text:
                    return el.text.strip()
            return ""

        # Número
        numero = buscar_tag(['nNF', 'Numero', 'numeroNota', 'nNFse'])
        if numero: dados["Número da Nota"] = numero.lstrip('0')

        # CNPJ e Fornecedor (Garante que pega do Prestador/Emitente)
        prestador = root.find('.//Prestador') or root.find('.//emit') or root.find('.//PrestadorServico')
        if prestador is not None:
            dados["CNPJ"] = buscar_tag(['CNPJ', 'Cnpj'], prestador)
            dados["Fornecedor"] = buscar_tag(['xFant', 'nomeFantasia', 'xNome', 'RazaoSocial'], prestador)
        else:
            dados["CNPJ"] = buscar_tag(['CNPJ', 'Cnpj', 'cnpjPrestador'])
            dados["Fornecedor"] = buscar_tag(['xFant', 'nomeFantasia', 'xNome', 'RazaoSocial'])

        # Valor
        dados["Valor"] = buscar_tag(['vNF', 'ValorServicos', 'valorLiquido', 'vProd', 'vLiq'])

        # Data
        data = buscar_tag(['dhEmi', 'DataEmissao', 'dtEmissao', 'dEmi'])
        if data and len(data) >= 10:
            data = data[:10]
            if "-" in data:
                partes = data.split("-")
                if len(partes) == 3: dados["Data"] = f"{partes[2]}/{partes[1]}/{partes[0]}"
            else:
                dados["Data"] = data

    except Exception:
        pass
    return dados

def extrair_dados_pdf(conteudo_bytes):
    """Extração de PDF preservando o alinhamento visual (layout=True)."""
    dados = {"Número da Nota": "", "CNPJ": "", "Valor": "", "Data": "", "Fornecedor": ""}
    try:
        texto_layout = ""
        texto_simples = ""
        
        with pdfplumber.open(io.BytesIO(conteudo_bytes)) as pdf:
            for pagina in pdf.pages:
                # O layout=True impede que colunas diferentes se misturem na mesma linha
                t_layout = pagina.extract_text(layout=True)
                t_simples = pagina.extract_text()
                if t_layout: texto_layout += t_layout + "\n"
                if t_simples: texto_simples += t_simples + "\n"

        if not texto_simples.strip():
            return dados

        # 1. Número da Nota
        m_num = re.search(r'(?:Número(?: da Nota)?|Nº(?: da Nota)?|NFS-e|NF-e|Nota)\s*[:.]?\s*0*(\d{1,15})(?:\s|$)', texto_simples, re.IGNORECASE)
        if m_num:
            dados["Número da Nota"] = m_num.group(1)

        # 2. CNPJ
        # O primeiro CNPJ do documento costuma ser obrigatoriamente o do Prestador
        cnpjs = re.findall(r'\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}', texto_simples)
        if cnpjs:
            dados["CNPJ"] = cnpjs[0]

        # 3. Fornecedor (O grande vilão resolvido)
        # Como layout=True preserva espaços largos entre colunas, usamos \s{3,} para parar a captura antes que pegue lixo
        m_forn = re.search(r'(?:Razão Social|Nome/Razão Social|Nome Fantasia|Prestador de Serviços)\s*[:\n]+\s*([A-Z0-9À-ÖØ-öø-ÿ\.\-\&\s]+?)(?:\s{3,}|CNPJ|CPF|\n|$)', texto_layout, re.IGNORECASE)
        if m_forn:
            nome = m_forn.group(1).strip()
            # Limpa se tiver capturado o próprio título
            nome = re.sub(r'^(?:Razão Social|Nome Fantasia|Nome/Razão Social)[\s:]*', '', nome, flags=re.IGNORECASE)
            dados["Fornecedor"] = nome.strip(":-. ")

        # 4. Data
        m_data = re.search(r'\b(\d{2}/\d{2}/\d{4})\b', texto_simples)
        if m_data:
            dados["Data"] = m_data.group(1)

        # 5. Valor
        # Procura termos exatos de totais financeiros e pega o número em seguida
        m_valor = re.search(r'(?:Valor Total|Valor Líquido|Total da Nota|Valor do Serviço)\s*[:.]?\s*R?\$?\s*([\d\.]+(?:,\d{2}))', texto_simples, re.IGNORECASE)
        if m_valor:
            dados["Valor"] = m_valor.group(1)
        else:
            # Pega o último formato monetário R$ encontrado no documento
            valores = re.findall(r'R\$\s*([\d\.]+(?:,\d{2}))', texto_simples)
            if valores:
                dados["Valor"] = valores[-1]

    except Exception:
        pass
    
    return dados

# --- PROCESSADOR INDIVIDUAL ---

def processar_arquivo(item):
    nome_arquivo, conteudo = item
    nome_lower = nome_arquivo.lower()
    
    dados = None
    if nome_lower.endswith('.xml'):
        dados = extrair_dados_xml(conteudo)
    elif nome_lower.endswith('.pdf'):
        dados = extrair_dados_pdf(conteudo)
        
    if not dados:
        dados = {"Número da Nota": "", "CNPJ": "", "Valor": "", "Data": "", "Fornecedor": ""}
        
    # Adiciona o nome real do arquivo
    dados["Arquivo"] = os.path.basename(nome_arquivo)
    return dados

# --- INTERFACE DO STREAMLIT ---

st.title("⚡ Extrator Exato de Notas Fiscais (XML / PDF)")
st.write("Anexe arquivos soltos ou um arquivo **.ZIP** contendo as notas fiscais.")

arquivos_carregados = st.file_uploader(
    "Escolha os arquivos XML/PDF ou um arquivo ZIP", 
    type=["xml", "pdf", "zip"], 
    accept_multiple_files=True
)

if arquivos_carregados:
    lista_arquivos = []
    
    with st.spinner("Lendo arquivos enviados..."):
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
        st.info(f"Total de {total_arquivos} notas detectadas. Iniciando processamento...")
        
        barra_progresso = st.progress(0)
        resultados = []
        max_workers = min(32, (os.cpu_count() or 4) * 4) 
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for i, resultado in enumerate(executor.map(processar_arquivo, lista_arquivos)):
                resultados.append(resultado)
                barra_progresso.progress((i + 1) / total_arquivos)

        if resultados:
            df = pd.DataFrame(resultados)
            
            # Colunas restritas à sua solicitação
            colunas_ordem = ["Número da Nota", "CNPJ", "Valor", "Data", "Fornecedor", "Arquivo"]
            for col in colunas_ordem:
                if col not in df.columns:
                    df[col] = ""
            df = df[colunas_ordem]
            
            st.success(f"Processamento concluído para {total_arquivos} notas.")
            
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
    else:
        st.warning("Nenhum arquivo válido (.xml ou .pdf) foi encontrado.")
