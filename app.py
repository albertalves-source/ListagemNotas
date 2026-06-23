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

# --- FUNÇÕES AUXILIARES DE SUPORTE ---

def extrair_numero_do_nome_arquivo(nome_arquivo):
    """Tenta extrair o número da nota a partir do nome do arquivo caso falhe no texto."""
    # Remove extensões
    nome_limpo = os.path.splitext(nome_arquivo)[0]
    # Procura por sequências de números isolados ou após prefixos comuns
    match = re.search(r'(?:NF|NF-e|NFS-e|NOTA|Nº)?\s*[_ \-]*(\d+)', nome_limpo, re.IGNORECASE)
    if match and len(match.group(1)) <= 9: # Notas fiscais geralmente têm até 9 dígitos
        return match.group(1)
    # Se não bater no padrão acima, pega o maior grupo de números puro encontrado
    numeros = re.findall(r'\d+', nome_limpo)
    if numeros:
        maior_num = max(numeros, key=len)
        if len(maior_num) <= 9:
            return maior_num
    return ""

# --- FUNÇÕES DE EXTRAÇÃO DE DADOS ---

def extrair_dados_xml(conteudo_bytes, nome_arquivo):
    """Extrai dados de um XML de NF-e/NFS-e padrão."""
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

        numero = buscar_tag(['nNF', 'Numero', 'numeroNota', 'nNFse', 'numNota'])
        cnpj = buscar_tag(['CNPJ', 'Cnpj', 'cnpjPrestador', 'cnpjEmitente', 'chNFe'])
        fornecedor = buscar_tag(['xNome', 'RazaoSocial', 'nomePrestador', 'nomeEmitente', 'xFant'])
        valor = buscar_tag(['vNF', 'ValorServicos', 'valorLiquido', 'vProd', 'vBC', 'vLiq'])
        data = buscar_tag(['dhEmi', 'DataEmissao', 'dtEmissao', 'dEmi', 'dhCompetencia', 'dtEmi'])
        
        if data and len(data) >= 10:
            data = data[:10]

        if cnpj and len(cnpj) == 44:
            cnpj = cnpj[6:20]

        if not numero or numero == "0":
            numero = extrair_numero_do_nome_arquivo(nome_arquivo)

        return {
            "Número da Nota": numero if numero else "Não identificado",
            "CNPJ": cnpj if cnpj else "Não identificado",
            "Valor": valor if valor else "0,00",
            "Data": data if data else "Não identificada",
            "Fornecedor": fornecedor if fornecedor else "Não identificado"
        }
    except Exception:
        return None

def extrair_dados_pdf(conteudo_bytes, nome_arquivo):
    """Extrai dados de um PDF usando padrões Regex Avançados e Hierárquicos."""
    try:
        pdf = PdfReader(io.BytesIO(conteudo_bytes))
        texto = ""
        for page in pdf.pages:
            texto += page.extract_text() or ""
            
        if not texto.strip():
            return {
                "Número da Nota": extrair_numero_do_nome_arquivo(nome_arquivo) or "PDF Sem Texto (Imagem)",
                "CNPJ": "Requer OCR",
                "Valor": "0,00",
                "Data": "Requer OCR",
                "Fornecedor": "PDF Escaneado"
            }

        # 1. Busca Avançada de Número da Nota
        numero = ""
        padroes_numero = [
            r'(?:NÚMERO|NUMERO|Nº|Nota Nº|Nota Fiscal Eletrônica|NFS-e|NF-e)\s*[:.]?\s*(\d+)',
            r'(?:Nº\s*DA\s*NOTA|NÚMERO\s*DA\s*NOTA)\s*[:.]?\s*(\d+)',
            r'(?:Fatura|Duplicata|Sequência)\s*[:.]?\s*(\d+)',
            r'\b\d{1,9}\b' # Procura por qualquer número isolado de tamanho compatível se falhar
        ]
        for padrao in padroes_numero:
            match = re.search(padrao, texto, re.IGNORECASE)
            if match and match.group(1) and match.group(1) != "0":
                numero = match.group(1)
                break
        
        if not numero or numero == "0":
            numero = extrair_numero_do_nome_arquivo(nome_arquivo)

        # 2. Busca de CNPJ
        cnpj_match = re.search(r'(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}|\d{14})', texto)
        cnpj = cnpj_match.group(1) if cnpj_match else "Não encontrado"

        # 3. Busca de Valor Monetário
        valor_match = re.search(r'(?:VALOR TOTAL|VALOR LÍQUIDO|VALOR LIQUIDO|TOTAL DA NOTA|R\$)\s*[:.]?\s*([\d.,]+)', texto, re.IGNORECASE)
        valor = valor_match.group(1) if valor_match else ""
        if not valor:
            valores_encontrados = re.findall(r'R\$\s*([\d.,]+)', texto)
            if valores_encontrados:
                valor = valores_encontrados[-1] # Geralmente o total fica no final do documento
        if not valor:
            valor = "Verificar no PDF"

        # 4. Busca de Data
        data_match = re.search(r'(\d{2}/\d{2}/\d{4}|\d{4}-\d{2}-\d{2})', texto)
        data = data_match.group(1) if data_match else "Não encontrada"

        # 5. Busca do Fornecedor (Nome/Razão Social)
        fornecedor = ""
        padroes_fornecedor = [
            r'(?:Razão Social|Razao Social|Prestador de Serviços|Prestador|Emitente|Nome/Razão Social)\s*[:.]?\s*([^\n\t\:]+)',
            r'(?:Razão Social\s*Do\s*Prestador)\s*[:.]?\s*([^\n\t\:]+)'
        ]
        for padrao in padroes_fornecedor:
            match = re.search(padrao, texto, re.IGNORECASE)
            if match and len(match.group(1).strip()) > 3:
                fornecedor = match.group(1).strip()
                break
        
        if not fornecedor or "verificar" in fornecedor.lower() or len(fornecedor) < 3:
            # Pega a primeira linha de texto significativa caso os padrões falhem (comum em topos de NFS-e)
            linhas = [l.strip() for l in texto.split('\n') if len(l.strip()) > 5]
            fornecedor = linhas[0] if linhas else "Verificar no PDF"

        # Limpezas básicas no nome do fornecedor para remover restos de texto
        fornecedor = re.sub(r'(CNPJ|Inscrição|Telefone|Endereço).*$', '', fornecedor, flags=re.IGNORECASE).strip()

        return {
            "Número da Nota": numero if numero else "Não identificado",
            "CNPJ": cnpj,
            "Valor": valor,
            "Data": data,
            "Fornecedor": fornecedor
        }
    except Exception:
        return {
            "Número da Nota": extrair_numero_do_nome_arquivo(nome_arquivo) or "Erro de Leitura",
            "CNPJ": "Erro",
            "Valor": "0,00",
            "Data": "Erro",
            "Fornecedor": "Falha ao processar estrutura"
        }

# --- PROCESSADOR INDIVIDUAL ---

def processar_arquivo(item):
    """Processa obrigatoriamente o arquivo e retorna uma linha estruturada."""
    nome_arquivo, conteudo = item
    nome_lower = nome_arquivo.lower()
    
    dados = None
    if nome_lower.endswith('.xml'):
        dados = extrair_dados_xml(conteudo, nome_arquivo)
    elif nome_lower.endswith('.pdf'):
        dados = extrair_dados_pdf(conteudo, nome_arquivo)
        
    if not dados:
        dados = {
            "Número da Nota": extrair_numero_do_nome_arquivo(nome_arquivo) or "Não identificado",
            "CNPJ": "Formato inválido",
            "Valor": "0,00",
            "Data": "Incompatível",
            "Fornecedor": "Não identificado"
        }
        
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
            
            # Colunas unificadas (sem Razão Social duplicada)
            colunas_ordem = ["Número da Nota", "CNPJ", "Valor", "Data", "Fornecedor", "Arquivo"]
            for col in colunas_ordem:
                if col not in df.columns:
                    df[col] = ""
            df = df[colunas_ordem]
            
            st.success(f"Sucesso! {len(df)} de {total_arquivos} linhas geradas.")
            
            # Pré-visualização da Tabela
            st.subheader("📋 Pré-visualização dos Dados")
            st.dataframe(df, use_container_width=True)
            
            # Geração do Excel para Download
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
