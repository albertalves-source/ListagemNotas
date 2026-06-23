import streamlit as st
import pandas as pd
import xml.etree.ElementTree as ET
import pdfplumber
import zipfile
import io
import re
import os
from concurrent.futures import ThreadPoolExecutor

st.set_page_config(page_title="Extrator de Notas Fiscais (Motor Espacial)", layout="wide")

# --- MOTOR DE RECONSTRUÇÃO ESPACIAL ---

def extrair_texto_espacial(pagina):
    """
    Extrai as palavras do PDF pelas coordenadas matemáticas (X, Y).
    Isso impede que colunas distantes se misturem na mesma linha.
    """
    palavras = pagina.extract_words()
    if not palavras:
        return []
    
    # Ordena de cima para baixo
    palavras.sort(key=lambda w: w['top'])
    
    linhas = []
    linha_atual = []
    top_atual = palavras[0]['top']
    
    # Agrupa palavras que estão na mesma altura (tolerância de 4 pixels)
    for p in palavras:
        if abs(p['top'] - top_atual) <= 4:
            linha_atual.append(p)
        else:
            # Ordena a linha da esquerda para a direita
            linha_atual.sort(key=lambda w: w['x0'])
            linhas.append(" ".join([w['text'] for w in linha_atual]))
            linha_atual = [p]
            top_atual = p['top']
            
    if linha_atual:
        linha_atual.sort(key=lambda w: w['x0'])
        linhas.append(" ".join([w['text'] for w in linha_atual]))
        
    return linhas

def eh_texto_lixo(texto):
    """Verifica se a linha é um rótulo do governo e não o nome da empresa."""
    lixos = ["PREFEITURA", "MUNICÍPIO", "SECRETARIA", "NOTA FISCAL", "NFS-E", 
             "DANFE", "DOCUMENTO", "DADOS", "PRESTADOR", "EMITENTE", "TOMADOR"]
    texto_upper = texto.upper()
    for lixo in lixos:
        if lixo in texto_upper:
            return True
    return False

# --- FUNÇÕES DE EXTRAÇÃO DE DADOS ---

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
    dados = {"Número da Nota": "", "CNPJ": "", "Valor": "", "Data": "", "Fornecedor": ""}
    linhas = []
    texto_corrido = ""

    try:
        with pdfplumber.open(io.BytesIO(conteudo_bytes)) as pdf:
            for pagina in pdf.pages:
                linhas_pagina = extrair_texto_espacial(pagina)
                linhas.extend(linhas_pagina)
                texto_corrido += "\n".join(linhas_pagina) + "\n"
    except Exception:
        return dados

    if not linhas:
        return dados

    # 1. CNPJ DO FORNECEDOR (O primeiro válido no documento)
    cnpjs = re.findall(r'\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b', texto_corrido)
    if cnpjs:
        dados["CNPJ"] = cnpjs[0]

    # 2. FORNECEDOR
    fornecedor = ""
    for i, linha in enumerate(linhas):
        # Procura a âncora do rótulo
        m_razao = re.search(r'(?:Razão Social|Nome Fantasia|Nome/Razão Social|Nome Empresarial)\s*[:\-]?\s*(.+)', linha, re.IGNORECASE)
        if m_razao:
            cand = m_razao.group(1).strip()
            # Se a linha não tiver CNPJ ou Endereço grudado e for maior que 3 letras
            if len(cand) > 3 and not re.search(r'(?:CNPJ|CPF|Endereço|Inscrição|CEP)', cand, re.IGNORECASE):
                fornecedor = cand
                break
        
        # Se encontrou o bloco do emitente, o nome costuma estar na linha de baixo
        if re.search(r'^(?:PRESTADOR DE SERVIÇOS|EMITENTE DA NOTA|DADOS DO PRESTADOR)', linha, re.IGNORECASE):
            if i + 1 < len(linhas):
                cand_abaixo = linhas[i+1].strip()
                if len(cand_abaixo) > 3 and not eh_texto_lixo(cand_abaixo) and not re.search(r'(?:CNPJ|CPF)', cand_abaixo):
                    # Tira rótulos se vieram grudados
                    cand_abaixo = re.sub(r'^(?:Razão Social|Nome Fantasia)[\s:]*', '', cand_abaixo, flags=re.IGNORECASE).strip()
                    fornecedor = cand_abaixo
                    break

    # Se não achou por rótulo, usa o CNPJ como âncora e pega a linha de cima
    if not fornecedor and dados["CNPJ"]:
        for i, linha in enumerate(linhas):
            if dados["CNPJ"] in linha and i > 0:
                cand_acima = linhas[i-1].strip()
                if not eh_texto_lixo(cand_acima) and len(cand_acima) > 3:
                    cand_acima = re.sub(r'^(?:Razão Social|Nome Fantasia)[\s:]*', '', cand_acima, flags=re.IGNORECASE).strip()
                    fornecedor = cand_acima
                    break
    
    dados["Fornecedor"] = fornecedor.strip(" -:.,") if fornecedor else ""

    # 3. NÚMERO DA NOTA
    # Notas padrão e NFS-e Nacional
    for linha in linhas[:20]:
        m = re.search(r'(?:Número da Nota|Número NFS-e|Nº|Nota Nº|NFS-e|Número do Documento)[\s\:\-\.]*0*(\d+)', linha, re.IGNORECASE)
        if m:
            dados["Número da Nota"] = m.group(1)
            break
            
    # Se não achou na busca estrutural, varre o texto corrido
    if not dados["Número da Nota"]:
        m_num = re.search(r'Nº[\s]*0*(\d+)', texto_corrido, re.IGNORECASE)
        if m_num: dados["Número da Nota"] = m_num.group(1)

    # 4. DATA
    m_data = re.search(r'\b(\d{2}/\d{2}/\d{4})\b', texto_corrido)
    if m_data:
        dados["Data"] = m_data.group(1)

    # 5. VALOR
    for linha in linhas:
        m_val = re.search(r'(?:Valor Total|Total da Nota|Valor Líquido|Valor dos Serviços)[\s\:\.]*R?\$?\s*([\d\.]+(?:,\d{2}))', linha, re.IGNORECASE)
        if m_val:
            dados["Valor"] = m_val.group(1)
            break
            
    if not dados["Valor"]:
        valores = re.findall(r'R\$\s*([\d\.]+(?:,\d{2}))', texto_corrido)
        if valores:
            dados["Valor"] = valores[-1]

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
        
    dados["Arquivo"] = os.path.basename(nome_arquivo)
    return dados

# --- INTERFACE DO STREAMLIT ---

st.title("⚡ Extrator de Notas Fiscais (Motor Espacial)")
st.write("Anexe arquivos soltos ou um arquivo **.ZIP**. Este extrator utiliza coordenadas visuais para impedir que colunas se misturem.")

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
        max_workers = min(16, (os.cpu_count() or 2) * 4) 
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for i, resultado in enumerate(executor.map(processar_arquivo, lista_arquivos)):
                resultados.append(resultado)
                barra_progresso.progress((i + 1) / total_arquivos)

        if resultados:
            df = pd.DataFrame(resultados)
            
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
