import streamlit as st
import pandas as pd
import xml.etree.ElementTree as ET
import pdfplumber
import zipfile
import io
import re
from concurrent.futures import ThreadPoolExecutor
import os

st.set_page_config(page_title="Extrator Exato de Notas Fiscais", layout="wide")

# --- FUNÇÕES DE EXTRAÇÃO EXATA (SEM TEXTOS GENÉRICOS) ---

def extrair_dados_xml(conteudo_bytes):
    """Extrai apenas os dados presentes no XML, sem inventar valores."""
    dados = {"Número da Nota": "", "CNPJ": "", "Valor": "", "Data": "", "Fornecedor": ""}
    try:
        xml_str = conteudo_bytes.decode('utf-8', errors='ignore')
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
            data = data[:10]
            if "-" in data:
                p = data.split("-")
                if len(p) == 3: dados["Data"] = f"{p[2]}/{p[1]}/{p[0]}"
            else:
                dados["Data"] = data
    except Exception:
        pass
    return dados

def extrair_dados_pdf(conteudo_bytes):
    """Lê o PDF linha a linha e extrai somente onde há correspondência exata."""
    dados = {"Número da Nota": "", "CNPJ": "", "Valor": "", "Data": "", "Fornecedor": ""}
    try:
        texto_completo = ""
        linhas = []
        with pdfplumber.open(io.BytesIO(conteudo_bytes)) as pdf:
            for pagina in pdf.pages:
                # Extração simples: quebra as linhas como o olho humano lê a tabela
                t = pagina.extract_text()
                if t:
                    texto_completo += t + "\n"
                    linhas.extend([l.strip() for l in t.split('\n') if l.strip()])

        if not linhas:
            return dados

        # 1. FORNECEDOR (Busca em 3 Passos Estritos)
        fornecedor_encontrado = ""
        
        # Passo A: Rótulo e Nome na MESMA linha (ex: "Razão Social: EMPRESA LTDA")
        for linha in linhas:
            m = re.search(r'(?:Razão Social|Nome Fantasia|Nome/Razão Social|Nome Comercial|Emitente)\s*[:\-]\s*(.+)', linha, re.IGNORECASE)
            if m:
                n = m.group(1)
                # Se o PDF juntou colunas, corta onde começa CNPJ, Inscrição, etc.
                n = re.split(r'\s{2,}|CNPJ|CPF|Inscrição|Endereço', n, flags=re.IGNORECASE)[0].strip()
                if len(n) > 3:
                    fornecedor_encontrado = n
                    break
                    
        # Passo B: Nome na linha SEGUINTE ao rótulo
        if not fornecedor_encontrado:
            for i, linha in enumerate(linhas):
                if re.search(r'^(?:Razão Social|Nome Fantasia|Nome/Razão Social|Nome Comercial)\s*[:\-]?\s*$', linha, re.IGNORECASE):
                    if i + 1 < len(linhas):
                        prox = linhas[i+1]
                        if not re.search(r'^(?:CNPJ|CPF|Inscrição|Endereço|CEP|Município)', prox, re.IGNORECASE):
                            fornecedor_encontrado = prox
                            break
                            
        # Passo C: Dentro do BLOCO "Prestador de Serviços"
        if not fornecedor_encontrado:
            for i, linha in enumerate(linhas):
                if re.search(r'^(?:PRESTADOR DE SERVIÇOS|DADOS DO PRESTADOR|EMITENTE|IDENTIFICAÇÃO DO PRESTADOR)', linha, re.IGNORECASE):
                    for j in range(1, 4):
                        if i + j < len(linhas):
                            prox = linhas[i+j]
                            # Ignora linhas em branco ou se for o próprio rótulo repetido
                            if not prox or re.search(r'(?:Razão Social|Nome Fantasia|Nome/Razão Social)', prox, re.IGNORECASE):
                                continue
                            # O primeiro texto livre que não seja documento/endereço é o nome
                            if not re.search(r'^(?:CNPJ|CPF|Inscrição|Endereço|CEP|Município|Telefone|E-mail)', prox, re.IGNORECASE):
                                fornecedor_encontrado = prox
                                break
                    if fornecedor_encontrado:
                        break

        if fornecedor_encontrado:
            # Limpa qualquer pontuação sobrando no fim
            dados["Fornecedor"] = fornecedor_encontrado.strip(":-. ")

        # 2. CNPJ
        cnpjs = re.findall(r'\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}', texto_completo)
        if cnpjs:
            dados["CNPJ"] = cnpjs[0]

        # 3. NÚMERO DA NOTA
        for linha in linhas:
            m = re.search(r'(?:Número da Nota|Número|Nº da Nota|Nota Nº|NFS-e|NF-e)\s*[:\-]?\s*0*(\d+)', linha, re.IGNORECASE)
            if m:
                dados["Número da Nota"] = m.group(1)
                break
        if not dados["Número da Nota"]:
            m2 = re.search(r'Nº\s*0*(\d+)', texto_completo, re.IGNORECASE)
            if m2:
                dados["Número da Nota"] = m2.group(1)

        # 4. VALOR
        for linha in linhas:
            m = re.search(r'(?:Valor Total|Valor Líquido|Total da Nota|Valor do Serviço)\s*[:\-]?\s*R?\$?\s*([\d\.]+(?:,\d{2}))', linha, re.IGNORECASE)
            if m:
                dados["Valor"] = m.group(1)
                break
        if not dados["Valor"]:
            valores = re.findall(r'R\$\s*([\d\.]+(?:,\d{2}))', texto_completo)
            if valores:
                dados["Valor"] = valores[-1]

        # 5. DATA
        m_data = re.search(r'\b(\d{2}/\d{2}/\d{4})\b', texto_completo)
        if m_data:
            dados["Data"] = m_data.group(1)

    except Exception:
        pass
    return dados

# --- PROCESSADOR INDIVIDUAL ---

def processar_arquivo(item):
    nome_arquivo, conteudo = item
    nome_lower = nome_arquivo.lower()
    
    # Inicia com os dados completamente limpos/vazios
    dados = None
    if nome_lower.endswith('.xml'):
        dados = extrair_dados_xml(conteudo)
    elif nome_lower.endswith('.pdf'):
        dados = extrair_dados_pdf(conteudo)
        
    if not dados:
        dados = {"Número da Nota": "", "CNPJ": "", "Valor": "", "Data": "", "Fornecedor": ""}
        
    # Anexa o nome do arquivo, como solicitado
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
            
            # Ordena e garante que apenas as colunas solicitadas existam
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
