import streamlit as st
import pandas as pd
import xml.etree.ElementTree as ET
import pdfplumber
import zipfile
import io
import re
import os
from concurrent.futures import ThreadPoolExecutor

st.set_page_config(page_title="Extrator de Notas Fiscais (Leitura Profunda)", layout="wide")

# --- LISTA NEGRA DE PALAVRAS (Evita pegar prefeituras e lixos) ---
BLACKLIST_FORNECEDOR = [
    "PREFEITURA", "MUNICÍPIO", "MUNICIPIO", "SECRETARIA", "ESTADO", "GOVERNO",
    "NOTA FISCAL", "ELETRÔNICA", "ELETRONICA", "NFS-E", "NF-E", "DANFE",
    "DOCUMENTO", "AUXILIAR", "IMPOSTO", "TRIBUTO", "TOMADOR", "DESTINATÁRIO",
    "COMPROVANTE", "RECIBO", "DADOS DO", "PRESTADOR", "EMITENTE", "SERVIÇO",
    "ENDEREÇO", "BAIRRO", "CEP", "MUNICÍPIO", "UF", "TELEFONE", "E-MAIL"
]

def texto_valido_para_fornecedor(texto):
    """Verifica se o texto capturado não é um título ou lixo do layout."""
    texto_upper = texto.upper()
    if len(texto) < 3:
        return False
    # Se contém qualquer palavra da lista negra, é lixo
    for palavra in BLACKLIST_FORNECEDOR:
        if palavra in texto_upper:
            return False
    # Se for só números ou começar com endereço, é lixo
    if re.search(r'^\d+$', texto) or re.search(r'^(RUA|AV|AVENIDA|ALAMEDA|TRAVESSA|RODOVIA|LOTE|QDA|QUADRA)', texto_upper):
        return False
    return True

# --- FUNÇÕES DE EXTRAÇÃO ---

def extrair_dados_xml(conteudo_bytes):
    """Extração 100% precisa das tags do XML."""
    dados = {"Número da Nota": "", "CNPJ": "", "Valor": "", "Data": "", "Fornecedor": ""}
    try:
        xml_str = conteudo_bytes.decode('utf-8', errors='ignore')
        xml_str = re.sub(r'xmlns="[^"]*"', '', xml_str) # Remove namespaces para não atrapalhar
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

        # Garante que pega do Prestador e não do Tomador
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
    """Leitura profunda do PDF ancorada pelo CNPJ."""
    dados = {"Número da Nota": "", "CNPJ": "", "Valor": "", "Data": "", "Fornecedor": ""}
    texto_completo = ""
    linhas = []

    try:
        with pdfplumber.open(io.BytesIO(conteudo_bytes)) as pdf:
            for pagina in pdf.pages:
                t = pagina.extract_text()
                if t: 
                    texto_completo += t + "\n"
                    # Quebra mantendo a ordem original da leitura
                    linhas.extend([l.strip() for l in t.split('\n') if l.strip()])
    except Exception:
        return dados # Se falhar ao abrir, retorna vazio

    if not linhas:
        return dados # PDF sem texto copiável (imagem)

    # 1. CNPJ DO FORNECEDOR
    # O primeiro CNPJ do documento em uma nota de serviço é invariavelmente o do Prestador
    cnpjs = re.findall(r'\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b', texto_completo)
    if cnpjs:
        dados["CNPJ"] = cnpjs[0]

    # 2. FORNECEDOR (Ancorado no CNPJ)
    if dados["CNPJ"]:
        for i, linha in enumerate(linhas):
            if dados["CNPJ"] in linha:
                # Estratégia A: O nome está na mesma linha, antes do CNPJ
                texto_antes = linha.split(dados["CNPJ"])[0].strip()
                texto_antes = re.sub(r'^(?:Razão Social|Nome Fantasia|Prestador|Emitente|Nome)[\s:\-\.]*', '', texto_antes, flags=re.IGNORECASE).strip()
                texto_antes = re.sub(r'(?:CNPJ|CPF)[\s:\-\.]*$', '', texto_antes, flags=re.IGNORECASE).strip()
                
                if texto_valido_para_fornecedor(texto_antes):
                    dados["Fornecedor"] = texto_antes
                    break
                
                # Estratégia B: O nome está na linha imediatamente ACIMA do CNPJ
                if i > 0:
                    linha_cima = linhas[i-1]
                    linha_cima = re.sub(r'^(?:Razão Social|Nome Fantasia|Prestador|Emitente|Nome)[\s:\-\.]*', '', linha_cima, flags=re.IGNORECASE).strip()
                    if texto_valido_para_fornecedor(linha_cima):
                        dados["Fornecedor"] = linha_cima
                        break
                        
                # Estratégia C: O nome está duas linhas ACIMA do CNPJ (comum em tabelas quebradas)
                if i > 1:
                    linha_cima_dois = linhas[i-2]
                    linha_cima_dois = re.sub(r'^(?:Razão Social|Nome Fantasia|Prestador|Emitente|Nome)[\s:\-\.]*', '', linha_cima_dois, flags=re.IGNORECASE).strip()
                    if texto_valido_para_fornecedor(linha_cima_dois):
                        dados["Fornecedor"] = linha_cima_dois
                        break

    # 3. NÚMERO DA NOTA
    # Procura estritamente por rótulos de número de nota perto do topo do documento
    for linha in linhas[:30]: 
        m = re.search(r'(?:N[UÚ]MERO|N[uú]mero da Nota|Nº|NF-e|NFS-e|Nota)[\s:\-\.]*0*(\d{1,15})\b', linha, re.IGNORECASE)
        if m:
            dados["Número da Nota"] = m.group(1)
            break

    # 4. VALOR
    # Busca pela palavra "Total" ou "Líquido"
    for linha in linhas:
        m = re.search(r'(?:Total|Líquido|Liquido|Valor da Nota|Valor do Serviço)[\s:\-\.]*R?\$?\s*([\d\.]+(?:,\d{2}))', linha, re.IGNORECASE)
        if m:
            dados["Valor"] = m.group(1)
            break
    # Se não achou com rótulo, pega o último valor monetário do documento
    if not dados["Valor"]:
        valores = re.findall(r'R\$\s*([\d\.]+(?:,\d{2}))', texto_completo)
        if valores:
            dados["Valor"] = valores[-1]

    # 5. DATA
    m_data = re.search(r'\b(\d{2}/\d{2}/\d{4})\b', texto_completo)
    if m_data:
        dados["Data"] = m_data.group(1)

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

st.title("⚡ Extrator de Notas Fiscais (Leitura Profunda)")
st.write("Anexe arquivos soltos ou um arquivo **.ZIP** contendo as notas fiscais. O sistema fará uma leitura cautelosa de cada arquivo.")

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
        st.info(f"Total de {total_arquivos} notas detectadas. Iniciando processamento cuidadoso...")
        
        barra_progresso = st.progress(0)
        resultados = []
        
        # Reduzido o número de processos paralelos para garantir estabilidade na leitura profunda
        max_workers = min(10, (os.cpu_count() or 2) * 2) 
        
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
