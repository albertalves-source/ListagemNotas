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

# --- FUNÇÕES DE LIMPEZA E VALIDAÇÃO EXATA ---

def limpar_numero_nota(texto_completo):
    """Busca o número da nota com base em termos estruturais fixos de layouts brasileiros."""
    # Padrão específico para WebISS e Notas Municipais: procura por "Número da Nota:" ou "Nº:"
    match = re.search(r'(?:NÚMERO\s*DA\s*NOTA|NUMERO\s*DA\s*NOTA|NOTA\s*Nº|Nº\s*DA\s*NOTA|Nº)\s*[:.]?\s*(\d+)', texto_completo, re.IGNORECASE)
    if match:
        num = match.group(1).lstrip('0')
        # Se após limpar os zeros o número fizer sentido (até 9 dígitos), retorna
        if num and len(num) <= 9:
            return num
            
    # Fallback secundário restrito para NF-e/NFS-e tradicionais
    match_se = re.search(r'\b(?:NF-e|NFS-e)\s*[:.]?\s*(\d+)\b', texto_completo, re.IGNORECASE)
    if match_se:
        return match_se.group(1).lstrip('0')
        
    return ""

def extrair_cnpj_prestador(texto_completo, linhas):
    """Localiza o primeiro CNPJ válido associado ao bloco do Prestador/Emitente."""
    # Encontra todos os CNPJs na nota
    cnpjs = re.findall(r'\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b|\b\d{14}\b', texto_completo)
    if not cnpjs:
        return ""
        
    # O CNPJ do fornecedor (prestador) quase sempre é o primeiro a aparecer no topo do documento
    cnpj_alvo = cnpjs[0]
    
    # Formata caso venha sem pontos
    cnpj_limpo = re.sub(r'\D', '', cnpj_alvo)
    if len(cnpj_limpo) == 14:
        return f"{cnpj_limpo[:2]}.{cnpj_limpo[2:5]}.{cnpj_limpo[5:8]}/{cnpj_limpo[8:12]}-{cnpj_limpo[12:]}"
    return cnpj_alvo

def extrair_fornecedor_exato(linhas):
    """Varre as linhas da nota localizando a âncora exata do bloco do fornecedor."""
    # Palavras que invalidam a linha de ser um fornecedor real
    termos_invalidos = ["PREFEITURA", "MUNICÍPIO", "MUNICIPIO", "SECRETARIA", "ESTADO", "TRIBUTOS", "FISCAL", "NFS-E", "TOMADOR"]
    
    for idx, linha in enumerate(linhas):
        linha_upper = linha.upper()
        
        # ÂNCORA 1: Identificou o bloco de início do Prestador
        if "PRESTADOR" in linha_upper or "EMITENTE" in linha_upper or "DADOS DO PRESTADOR" in linha_upper:
            # O nome da empresa geralmente está nas 3 linhas imediatamente abaixo do título do bloco
            for k in range(1, 4):
                if idx + k < len(linhas):
                    candidata = linhas[idx + k].strip()
                    # Verifica se a linha não está em branco e se não é um cabeçalho/endereço público
                    if candidata and len(candidata) > 3:
                        if not any(termo in candidata.upper() for termo in termos_invalidos):
                            # Se a linha começar com CNPJ ou Inscrição, não é o nome da empresa
                            if not re.search(r'^(?:CNPJ|CPF|INSCRIÇÃO|INSCRICAO|ENDEREÇO|LOGRADOURO|\d)', candidata, re.IGNORECASE):
                                return candidata
                                
        # ÂNCORA 2: Se vier no formato clássico "Razão Social: NOME DA EMPRESA"
        if "RAZÃO SOCIAL" in linha_upper or "RAZAO SOCIAL" in linha_upper or "NOME / RAZÃO SOCIAL" in linha_upper:
            candidata = re.sub(r'^.*(?:RAZÃO\s*SOCIAL|RAZAO\s*SOCIAL|NOME\s*/\s*RAZÃO\s*SOCIAL)\s*[:.]?\s*', '', linha, flags=re.IGNORECASE).strip()
            if candidata and len(candidata) > 3 and not any(termo in candidata.upper() for termo in termos_invalidos):
                return candidata
                
    # Fallback caso não ache nenhuma âncora: pega a primeira linha do topo que não seja lixo público
    for linha in linhas[:10]:
        candidata = linha.strip()
        if len(candidata) > 4 and not any(termo in candidata.upper() for termo in termos_invalidos):
            if not re.search(r'^(?:RUA|AV\.|AVENIDA|PRAÇA|PC\.|\d|CEP|TEL)', candidata, re.IGNORECASE):
                return candidata
                
    return ""

# --- FUNÇÕES PRINCIPAIS DE PROCESSAMENTO ---

def extrair_dados_xml(conteudo_bytes):
    """Extração pura baseada nas tags estruturais do XML."""
    try:
        xml_str = conteudo_bytes.decode('utf-8', errors='ignore')
        xml_str = re.sub(r'xmlns="[^"]*"', '', xml_str)
        root = ET.fromstring(xml_str)
        
        def buscar_tag(tags):
            for tag in tags:
                el = root.find(f".//{tag}")
                if el is not None and el.text:
                    return el.text.strip()
            return ""

        numero = buscar_tag(['nNF', 'Numero', 'numeroNota', 'nNFse'])
        cnpj = buscar_tag(['CNPJ', 'Cnpj', 'cnpjPrestador', 'cnpjEmitente'])
        
        # Prioriza Nome Fantasia, se não houver usa Razão Social
        fornecedor = buscar_tag(['xFant', 'nomeFantasia'])
        if not fornecedor:
            fornecedor = buscar_tag(['xNome', 'RazaoSocial', 'nomePrestador', 'nomeEmitente'])
            
        valor = buscar_tag(['vNF', 'ValorServicos', 'valorLiquido', 'vProd', 'vLiq'])
        data = buscar_tag(['dhEmi', 'DataEmissao', 'dtEmissao', 'dEmi', 'dtEmi'])
        
        if data and len(data) >= 10:
            data = data[:10]
            # Converte AAAA-MM-DD para DD/MM/AAAA se necessário
            if "-" in data:
                partes = data.split("-")
                if len(partes) == 3 and len(partes[0]) == 4:
                    data = f"{partes[2]}/{partes[1]}/{partes[0]}"

        # Limpeza fina de números de notas em XML
        if numero:
            numero = numero.lstrip('0')

        return {
            "Número da Nota": numero if numero else "",
            "CNPJ": formatar_cnpj(cnpj) if cnpj else "",
            "Valor": valor if valor else "0,00",
            "Data": data if data else "",
            "Fornecedor": fornecedor if fornecedor else ""
        }
    except Exception:
        return None

def extrair_dados_pdf(conteudo_bytes):
    """Abre a nota fiscal e processa linha por linha usando ancoragem estrita."""
    try:
        texto_completo = ""
        linhas = []
        
        with pdfplumber.open(io.BytesIO(conteudo_bytes)) as pdf:
            for pagina in pdf.pages:
                texto_pag = pagina.extract_text()
                if texto_pag:
                    texto_completo += texto_pag + "\n"
                    # Divide o PDF em linhas reais limpas
                    for l in texto_pag.split('\n'):
                        if l.strip():
                            linhas.append(l.strip())

        if not texto_completo.strip():
            return {
                "Número da Nota": "", "CNPJ": "", "Valor": "0,00", "Data": "", "Fornecedor": "PDF sem texto (Imagem/OCR requerido)"
            }

        # Executa as funções de ancoragem estrita
        numero = limpar_numero_nota(texto_completo)
        cnpj = extrair_cnpj_prestador(texto_completo, linhas)
        fornecedor = extrair_fornecedor_exato(linhas)
        
        # Captura de Valor formatado
        valor = ""
        valor_match = re.search(r'(?:VALOR TOTAL|VALOR LÍQUIDO|VALOR LIQUIDO|TOTAL DA NOTA|VALOR\s*DO\s*SERVIÇO|R\$)\s*[:.]?\s*([\d\.,]+)', texto_completo, re.IGNORECASE)
        if valor_match:
            valor = valor_match.group(1).strip().rstrip('.')
        else:
            todos_valores = re.findall(r'\b\d{1,3}(?:\.\d{3})*,\d{2}\b', texto_completo)
            if todos_valores:
                valor = todos_valores[-1]

        # Captura de Data pura (DD/MM/AAAA)
        data = ""
        data_match = re.search(r'\b(\d{2}/\d{2}/\d{4})\b', texto_completo)
        if data_match:
            data = data_match.group(1)

        return {
            "Número da Nota": numero,
            "CNPJ": cnpj,
            "Valor": valor if valor else "0,00",
            "Data": data,
            "Fornecedor": fornecedor
        }
    except Exception:
        return {
            "Número da Nota": "", "CNPJ": "", "Valor": "0,00", "Data": "", "Fornecedor": "Erro no processamento deste arquivo"
        }

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
        dados = {"Número da Nota": "", "CNPJ": "", "Valor": "0,00", "Data": "", "Fornecedor": "Formato não suportado"}
        
    dados["Arquivo"] = os.path.basename(nome_arquivo)
    return dados

# --- INTERFACE DO STREAMLIT ---

st.title("⚡ Extrator Ultrarápido de Notas Fiscais (XML / PDF)")
st.write("Anexe arquivos soltos ou um arquivo **.ZIP** contendo até milhares de notas fiscais.")

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
        st.info(f"Total de {total_arquivos} notas fiscais detectadas. Iniciando processamento...")
        
        barra_progresso = st.progress(0)
        resultados = []
        max_workers = min(32, (os.cpu_count() or 4) * 4) 
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for i, resultado in enumerate(executor.map(processar_arquivo, lista_arquivos)):
                resultados.append(resultado)
                barra_progresso.progress((i + 1) / total_arquivos)

        if resultados:
            df = pd.DataFrame(resultados)
            
            # Colunas exatas requeridas
            colunas_ordem = ["Número da Nota", "CNPJ", "Valor", "Data", "Fornecedor", "Arquivo"]
            for col in colunas_ordem:
                if col not in df.columns:
                    df[col] = ""
            df = df[colunas_ordem]
            
            st.success(f"Sucesso! {len(df)} de {total_arquivos} notas processadas.")
            
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
