import streamlit as st
import pandas as pd
import xml.etree.ElementTree as ET
import pdfplumber
import zipfile
import io
import re
import os
from concurrent.futures import ThreadPoolExecutor
import pytesseract
from pdf2image import convert_from_bytes

st.set_page_config(page_title="Extrator Exato de Notas Fiscais + OCR", layout="wide")

# --- FUNÇÕES DE EXTRAÇÃO ---

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

def parse_texto_pdf(texto):
    """Analisa o texto extraído buscando âncoras exatas."""
    dados = {"Número da Nota": "", "CNPJ": "", "Valor": "", "Data": "", "Fornecedor": ""}
    
    if not texto.strip():
        return dados

    # 1. Data (Primeira data no formato DD/MM/AAAA)
    datas = re.findall(r'\b\d{2}/\d{2}/\d{4}\b', texto)
    if datas: 
        dados["Data"] = datas[0]

    # 2. CNPJ (Primeiro CNPJ válido = Prestador)
    cnpjs = re.findall(r'\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b', texto)
    if cnpjs: 
        dados["CNPJ"] = cnpjs[0]

    # 3. Valor (Procura formato financeiro)
    valores_str = re.findall(r'R\$\s*([\d\.]+(?:,\d{2}))', texto)
    if valores_str:
        dados["Valor"] = valores_str[-1] # Geralmente o total líquido fica no fim da nota

    # 4. Número da Nota
    m_num = re.search(r'(?:N[UÚ]MERO|N[uú]mero da Nota|Nº|NF-e|NFS-e)[\s:\-\.]*0*(\d{1,15})\b', texto, re.IGNORECASE)
    if m_num:
        dados["Número da Nota"] = m_num.group(1)

    # 5. Fornecedor (Busca ancorada no CNPJ)
    linhas = [l.strip() for l in texto.split('\n') if l.strip()]
    for i, linha in enumerate(linhas):
        if dados["CNPJ"] and dados["CNPJ"] in linha:
            # Opção A: O nome está na mesma linha, antes do CNPJ
            texto_antes = linha.split(dados["CNPJ"])[0].strip()
            texto_antes = re.sub(r'^(?:Razão Social|Nome Fantasia|Prestador|Emitente)[\s:]*', '', texto_antes, flags=re.IGNORECASE).strip()
            
            if len(texto_antes) > 3 and not re.search(r'(?:CNPJ|CPF)', texto_antes, re.IGNORECASE):
                dados["Fornecedor"] = texto_antes
                break
                
            # Opção B: O nome está na linha de cima (muito comum)
            if i > 0:
                linha_cima = linhas[i-1]
                linha_cima = re.sub(r'^(?:Razão Social|Nome Fantasia|Prestador|Emitente)[\s:]*', '', linha_cima, flags=re.IGNORECASE).strip()
                # Verifica se a linha de cima não é lixo público
                if len(linha_cima) > 3 and not re.search(r'(?:Prefeitura|Município|Secretaria|Nota Fiscal)', linha_cima, re.IGNORECASE):
                    dados["Fornecedor"] = linha_cima
                    break

    # Limpeza final de sujeiras no nome do fornecedor
    if dados["Fornecedor"]:
        dados["Fornecedor"] = re.sub(r'\s*(?:CNPJ|CPF|Inscrição|Endereço|Município|CEP).*', '', dados["Fornecedor"], flags=re.IGNORECASE).strip(" -:.,")

    return dados

def extrair_dados_pdf(conteudo_bytes):
    """Tenta ler texto nativo. Se falhar, usa OCR."""
    texto_completo = ""
    
    # Tentativa 1: Leitura nativa rápida
    try:
        with pdfplumber.open(io.BytesIO(conteudo_bytes)) as pdf:
            for pagina in pdf.pages:
                t = pagina.extract_text()
                if t: texto_completo += t + "\n"
    except Exception:
        pass

    # Tentativa 2: OCR se o texto nativo for quase inexistente (Imagem escaneada)
    if len(texto_completo.strip()) < 50:
        try:
            # Converte o PDF para imagens e passa o Tesseract
            imagens = convert_from_bytes(conteudo_bytes, dpi=200)
            for img in imagens:
                # 'por' indica português
                texto_completo += pytesseract.image_to_string(img, lang='por') + "\n"
        except Exception as e:
            # Ignora erros de OCR caso o servidor ainda não tenha instalado as dependências
            pass

    return parse_texto_pdf(texto_completo)

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

st.title("⚡ Extrator Exato de Notas Fiscais (XML / PDF + OCR)")
st.write("Anexe arquivos soltos ou um arquivo **.ZIP** contendo as notas fiscais. **Aviso:** PDFs escaneados (imagens) levarão mais tempo devido ao processamento do OCR.")

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
        # O Streamlit Cloud tem limite de processamento. Mantemos threads altas, mas o OCR limitará a velocidade.
        max_workers = min(32, (os.cpu_count() or 4) * 4) 
        
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
