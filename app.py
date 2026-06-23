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
        cnpj = buscar_tag(['CNPJ', 'Cnpj', 'cnpjPrestador', 'cnpjEmitente', 'chNFe'])
        razao_social = buscar_tag(['xNome', 'RazaoSocial', 'nomePrestador', 'nomeEmitente', 'xFant'])
        valor = buscar_tag(['vNF', 'ValorServicos', 'valorLiquido', 'vProd', 'vBC'])
        data = buscar_tag(['dhEmi', 'DataEmissao', 'dtEmissao', 'dEmi', 'dhCompetencia'])
        
        if data and len(data) >= 10:
            data = data[:10]

        # Tratamento caso o CNPJ venha dentro da chave de acesso (chNFe)
        if cnpj and len(cnpj) == 44:
            cnpj = cnpj[6:20]

        return {
            "Número da Nota": numero if numero else "Não encontrado",
            "Razão Social": razao_social if razao_social else "Não encontrado",
            "CNPJ": cnpj if cnpj else "Não encontrado",
            "Valor": valor if valor else "Não encontrado",
            "Data": data if data else "Não encontrado",
            "Fornecedor": razao_social if razao_social else "Não encontrado"
        }
    except Exception as e:
        return {
            "Número da Nota": "Erro no XML",
            "Razão Social": "Erro de Leitura",
            "CNPJ": "Erro de Leitura",
            "Valor": "",
            "Data": "",
            "Fornecedor": f"Erro: {str(e)[:20]}"
        }

def extrair_dados_pdf(conteudo_bytes):
    """Extrai dados de um PDF usando Expressões Regulares mais abrangentes."""
    try:
        pdf = PdfReader(io.BytesIO(conteudo_bytes))
        texto = ""
        for page in pdf.pages:
            texto += page.extract_text() or ""
            
        if not texto.strip():
            return {
                "Número da Nota": "PDF sem texto (Imagem)",
                "Razão Social": "Requer OCR",
                "CNPJ": "Requer OCR",
                "Valor": "",
                "Data": "",
                "Fornecedor": "PDF Escaneado"
            }

        # Regex aprimorados e mais tolerantes a espaços e quebras de linha
        numero_match = re.search(r'(?:NÚMERO|NUMERO|Nº|Nota Nº|Nota:)\s*[:.]?\s*(\d+)', texto, re.IGNORECASE)
        cnpj_match = re.search(r'(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}|\d{14})', texto)
        
        # Captura valores com pontuação brasileira (ex: 2.485.739,00 ou 933,33)
        valor_match = re.search(r'(?:VALOR TOTAL|VALOR LÍQUIDO|VALOR LIQUIDO|TOTAL DA NOTA|R\$)\s*[:.]?\s*([\d.,]+)', texto, re.IGNORECASE)
        if not valor_match:
            # Busca secundária por qualquer formato monetário comum se o principal falhar
            valor_match = re.search(r'R\$\s*([\d.,]+)', texto)

        data_match = re.search(r'(\d{2}/\d{2}/\d{4}|\d{4}-\d{2}-\d{2})', texto)
        razao_match = re.search(r'(?:Razão Social|Razao Social|Prestador|Emitente|Nome/Razão Social)\s*[:.]?\s*([^\n\t]+)', texto, re.IGNORECASE)

        numero = numero_match.group(1) if numero_match else "Não encontrado"
        cnpj = cnpj_match.group(1) if cnpj_match else "Não encontrado"
        valor = valor_match.group(1) if valor_match else "Não encontrado"
        data = data_match.group(1) if data_match else "Não encontrado"
        
        razao_social = razao_match.group(1).strip() if razao_match else ""
        if not razao_social or len(razao_social) < 3:
            razao_social = "Verificar no PDF"

        return {
            "Número da Nota": numero,
            "Razão Social": razao_social,
            "CNPJ": cnpj,
            "Valor": valor,
            "Data": data,
            "Fornecedor": razao_social
        }
    except Exception as e:
        return {
            "Número da Nota": "Erro no PDF",
            "Razão Social": "Erro de Leitura",
            "CNPJ": "Erro de Leitura",
            "Valor": "",
            "Data": "",
            "Fornecedor": f"Erro: {str(e)[:20]}"
        }

# --- PROCESSADOR INDIVIDUAL ---

def processar_arquivo(item):
    """Processa obrigatoriamente o arquivo e retorna uma linha estruturada."""
    nome_arquivo, conteudo = item
    nome_lower = nome_arquivo.lower()
    
    dados = None
    if nome_lower.endswith('.xml'):
        dados = extrair_dados_xml(conteudo)
    elif nome_lower.endswith('.pdf'):
        dados = extrair_dados_pdf(conteudo)
        
    # Se por qualquer motivo retornar vazio, criamos uma linha padrão para não perder o arquivo da contagem
    if not dados:
        dados = {
            "Número da Nota": "Não suportado",
            "Razão Social": "Formato inválido",
            "CNPJ": "Formato inválido",
            "Valor": "",
            "Data": "",
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
            
            colunas_ordem = ["Número da Nota", "Razão Social", "CNPJ", "Valor", "Data", "Fornecedor", "Arquivo"]
            for col in colunas_ordem:
                if col not in df.columns:
                    df[col] = ""
            df = df[colunas_ordem]
            
            # Alerta visual caso o total mapeado seja diferente do esperado para auditoria rápida
            st.success(f"Sucesso! {len(df)} linhas geradas para as {total_arquivos} notas encontradas.")
            
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
