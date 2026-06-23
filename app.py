import streamlit as st
import pandas as pd
import xml.etree.ElementTree as ET
import pdfplumber
import zipfile
import io
import re
import os
from concurrent.futures import ThreadPoolExecutor

st.set_page_config(page_title="Extrator de Notas Fiscais (Layouts Mistos)", layout="wide")

# --- FUNÇÕES DE EXTRAÇÃO EXATA ---

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

def limpar_fornecedor(texto):
    """Remove sujeiras comuns capturadas junto com o nome do fornecedor."""
    # Corta o texto se ele juntou com colunas de CNPJ, Endereço ou CPF
    texto = re.split(r'\s{2,}|CNPJ|CPF|Inscrição|Endereço|Município|Telefone', texto, flags=re.IGNORECASE)[0]
    return texto.strip(" :-.,\n")

def extrair_dados_pdf(conteudo_bytes):
    dados = {"Número da Nota": "", "CNPJ": "", "Valor": "", "Data": "", "Fornecedor": ""}
    texto_simples = ""
    linhas = []

    try:
        with pdfplumber.open(io.BytesIO(conteudo_bytes)) as pdf:
            for pagina in pdf.pages:
                # Extrai o texto normal
                t = pagina.extract_text()
                if t:
                    texto_simples += t + "\n"
                    linhas.extend([l.strip() for l in t.split('\n') if l.strip()])
    except Exception:
        return dados 

    if not linhas:
        return dados

    # 1. CNPJ DO FORNECEDOR (O primeiro CNPJ da nota é sempre o Emitente/Prestador)
    cnpjs = re.findall(r'\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}', texto_simples)
    if cnpjs:
        dados["CNPJ"] = cnpjs[0]
    else:
        # Tenta achar CNPJ sem pontuação
        cnpjs_puros = re.findall(r'\b\d{14}\b', texto_simples)
        # Ignora se for chave de acesso (44 digitos)
        for c in cnpjs_puros:
            if not re.search(r'\b\d{44}\b', texto_simples):
                dados["CNPJ"] = f"{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}"
                break

    # 2. NÚMERO DA NOTA
    # Busca por rótulos variados que as prefeituras e o estado (NF-e) usam
    for linha in linhas[:30]: # Geralmente o número está no topo
        m = re.search(r'(?:Número da Nota|Número da NFS-e|NFS-e Número|Número do Documento|Nº da Nota|Nota Fiscal Nº|Nº|Número|NFS-e)[\s\:\-\.]*([0-9\.\-]+)', linha, re.IGNORECASE)
        if m:
            num_limpo = re.sub(r'\D', '', m.group(1)).lstrip('0')
            if num_limpo:
                dados["Número da Nota"] = num_limpo
                break

    # Fallback para DANFE (NF-e de produto) usando a chave de acesso de 44 dígitos
    if not dados["Número da Nota"]:
        chaves = re.findall(r'\b\d{44}\b', texto_simples)
        if chaves:
            chave = chaves[0]
            # O número da NF-e padrão fica entre os dígitos 25 e 33 da chave de acesso
            dados["Número da Nota"] = chave[25:34].lstrip('0')

    # 3. FORNECEDOR (Estratégia de Blocos)
    fornecedor = ""
    for i, linha in enumerate(linhas):
        # A. Procura a âncora direta na linha
        m_razao = re.search(r'(?:Razão Social|Nome/Razão Social|Nome Empresarial|Nome Fantasia)[\s\:\-\.]*(.+)', linha, re.IGNORECASE)
        if m_razao:
            nome_candidato = limpar_fornecedor(m_razao.group(1))
            if len(nome_candidato) > 3:
                fornecedor = nome_candidato
                break

        # B. Procura o bloco "Prestador" ou "Emitente" e desce a linha
        if re.search(r'^(?:PRESTADOR DE SERVI[ÇC]OS|EMITENTE|DADOS DO PRESTADOR|DADOS DO EMITENTE)', linha, re.IGNORECASE):
            for j in range(1, 4):
                if i + j < len(linhas):
                    linha_abaixo = linhas[i+j]
                    # Ignora se for um rótulo de outra coisa
                    if re.search(r'^(?:CNPJ|CPF|Inscrição|Endereço|Município|Telefone|E-mail)', linha_abaixo, re.IGNORECASE):
                        continue
                    # Ignora se for a palavra "Razão Social" sozinha
                    if re.search(r'^(?:Razão Social|Nome Fantasia)[\s\:]*$', linha_abaixo, re.IGNORECASE):
                        continue
                    
                    nome_candidato = limpar_fornecedor(linha_abaixo)
                    if len(nome_candidato) > 3:
                        fornecedor = nome_candidato
                        break
            if fornecedor:
                break

    # C. Fallback: Se achou o CNPJ, pega a linha imediatamente acima dele (MUITO comum em Salvador e afins)
    if not fornecedor and dados["CNPJ"]:
        for i, linha in enumerate(linhas):
            if dados["CNPJ"] in linha and i > 0:
                linha_acima = linhas[i-1]
                linha_acima = re.sub(r'^(?:Razão Social|Nome Fantasia|Prestador|Emitente|Nome)[\s\:\-\.]*', '', linha_acima, flags=re.IGNORECASE).strip()
                if len(linha_acima) > 3 and not re.search(r'(?:Prefeitura|Município|Secretaria|Nota Fiscal|Documento)', linha_acima, re.IGNORECASE):
                    fornecedor = limpar_fornecedor(linha_acima)
                    break

    dados["Fornecedor"] = fornecedor

    # 4. VALOR
    m_valor = re.search(r'(?:Valor Total da Nota|Valor Líquido|Total da NFS-e|Valor do Serviço|VALOR TOTAL|Total|Líquido)[\s\:\.\-]*R?\$?\s*([\d\.]+(?:,\d{2}))', texto_simples, re.IGNORECASE)
    if m_valor:
        dados["Valor"] = m_valor.group(1)
    else:
        valores = re.findall(r'R\$\s*([\d\.]+(?:,\d{2}))', texto_simples)
        if valores:
            dados["Valor"] = valores[-1]

    # 5. DATA
    m_data = re.search(r'\b(\d{2}/\d{2}/\d{4})\b', texto_simples)
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

st.title("⚡ Extrator de Notas Fiscais (Layouts Mistos)")
st.write("Processamento otimizado para DANFE (NF-e) e notas municipais (NFS-e) variadas.")

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
