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

# --- FUNÇÕES AUXILIARES DE SUPORTE ---

def limpar_numero_nota(num_str):
    if not num_str:
        return ""
    num_limpo = re.sub(r'\D', '', num_str)
    if not num_limpo:
        return ""
    if len(num_limpo) > 9:
        match = re.search(r'^(?:2023|2024|2025|2026|2027)0*([1-9]\d*)$', num_limpo)
        if match:
            return match.group(1)
        num_limpo = num_limpo.lstrip('0')
    return num_limpo if num_limpo else "Não identificado"

def extrair_numero_do_nome_arquivo(nome_arquivo):
    nome_limpo = os.path.splitext(nome_arquivo)[0]
    match = re.search(r'(?:NF|NF-e|NFS-e|NOTA|Nº)?\s*[_ \-]*([1-9]\d{0,8})\b', nome_limpo, re.IGNORECASE)
    if match:
        return match.group(1)
    return ""

def formatar_cnpj(cnpj_str):
    if not cnpj_str:
        return ""
    cnpj_limpo = re.sub(r'\D', '', cnpj_str)
    if len(cnpj_limpo) == 14:
        return f"{cnpj_limpo[:2]}.{cnpj_limpo[2:5]}.{cnpj_limpo[5:8]}/{cnpj_limpo[8:12]}-{cnpj_limpo[12:]}"
    return cnpj_str

def limpar_nome_fornecedor(nome):
    """Remove cabeçalhos e lixos comuns que sobram na célula do fornecedor."""
    if not nome:
        return ""
    # Remove quebras de linha e espaços excessivos
    nome = re.sub(r'\s+', ' ', nome).strip()
    # Remove labels comuns se eles vierem colados no nome
    substituicoes = [
        r'(?i)^.*Razão\s*Social:\s*', r'(?i)^.*Razao\s*Social:\s*', 
        r'(?i)^.*Nome\s*Fantasia:\s*', r'(?i)^.*Prestador\s*de\s*Serviços:\s*',
        r'(?i)^.*Nome\s*/\s*Razão\s*Social:\s*', r'(?i)CNPJ:.*$', r'(?i)Inscrição.*$'
    ]
    for pattern in substituicoes:
        nome = re.sub(pattern, '', nome).strip()
        
    # Limpa pontuações órfãs nas pontas
    return nome.strip(":-. ")

# --- FUNÇÕES DE EXTRAÇÃO DE DADOS ---

def extrair_dados_xml(conteudo_bytes, nome_arquivo):
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
        cnpj = buscar_tag(['CNPJ', 'Cnpj', 'cnpjPrestador', 'cnpjEmitente'])
        
        # Prioriza Nome Fantasia Comercial no XML
        fornecedor = buscar_tag(['xFant', 'nomeFantasia'])
        if not fornecedor or len(fornecedor) < 3:
            fornecedor = buscar_tag(['xNome', 'RazaoSocial', 'nomePrestador', 'nomeEmitente'])
        
        valor = buscar_tag(['vNF', 'ValorServicos', 'valorLiquido', 'vProd', 'vBC', 'vLiq'])
        data = buscar_tag(['dhEmi', 'DataEmissao', 'dtEmissao', 'dEmi', 'dhCompetencia', 'dtEmi'])
        
        if data and len(data) >= 10:
            data = data[:10]

        numero = limpar_numero_nota(numero)
        if not numero or numero == "Não identificado":
            numero = extrair_numero_do_nome_arquivo(nome_arquivo)

        return {
            "Número da Nota": numero if numero else "Não identificado",
            "CNPJ": formatar_cnpj(cnpj),
            "Valor": valor if valor else "0,00",
            "Data": data if data else "Não identificada",
            "Fornecedor": limpar_nome_fornecedor(fornecedor)
        }
    except Exception:
        return None

def extrair_dados_pdf(conteudo_bytes, nome_arquivo):
    try:
        texto_completo = ""
        fornecedor = ""
        cnpj = ""
        
        with pdfplumber.open(io.BytesIO(conteudo_bytes)) as pdf:
            for pagina in pdf.pages:
                texto_pag = pagina.extract_text()
                if texto_pag:
                    texto_completo += texto_pag + "\n"
                
                # --- ESTRATÉGIA BI-DIMENSIONAL (TABELAS) ---
                # Extrai as tabelas visuais desenhadas na nota (WebISS, Ginfes, Nota Paulista utilizam isso)
                tabelas = pagina.extract_tables()
                for tabela in tabelas:
                    for linha in tabela:
                        # Une os textos da linha para análise rápida
                        linha_texto = " ".join([str(celula) for celula in linha if celula])
                        
                        # Se achamos a célula do Prestador/Emitente
                        if re.search(r'(?:Prestador|Emitente|Razão\s*Social|Razao\s*Social|Nome\s*Fantasia)', linha_texto, re.IGNORECASE):
                            # Varre cada célula procurando o nome comercial real
                            for celula in linha:
                                if celula and len(celula.strip()) > 3:
                                    texto_celula = celula.strip()
                                    # Evita pegar títulos estáticos de colunas
                                    if not re.search(r'^(?:Prestador|Emitente|Tomador|Razão\s*Social|CNPJ/CPF|Inscrição|Endereço)$', texto_celula, re.IGNORECASE):
                                        # Filtra strings longas que contêm o nome da empresa
                                        linhas_celula = [l.strip() for l in texto_celula.split('\n') if l.strip()]
                                        for l_candidata in linhas_celula:
                                            if len(l_candidata) > 3 and not re.search(r'(?:PREFEITURA|MUNICÍPIO|SECRETARIA|CNPJ|TELEFONE|DADOS DO)', l_candidata, re.IGNORECASE):
                                                fornecedor = l_candidata
                                                break
                                if fornecedor: break
                        if fornecedor: break
                    if fornecedor: break

        # --- ESTRATÉGIA DE FALLBACK (CASO O PDF NÃO USE TABELAS PARSÁVEIS) ---
        linhas_texto = [l.strip() for l in texto_completo.split('\n') if l.strip()]
        
        # Captura de CNPJ de segurança
        cnpj_match = re.search(r'(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}|\d{14})', texto_completo)
        cnpj = formatar_cnpj(cnpj_match.group(1)) if cnpj_match else "Não encontrado"

        if not fornecedor:
            # Varredura inteligente por linhas que cercam palavras-chave
            for idx, linha in enumerate(linhas_texto):
                if re.search(r'(?:Nome\s*Fantasia|Nome\s*Comercial|Razão\s*Social|Razao\s*Social|Prestador de)', linha, re.IGNORECASE):
                    # Se houver texto à direita do rótulo na mesma linha
                    candidato = re.sub(r'^.*(?:Nome\s*Fantasia|Nome\s*Comercial|Razão\s*Social|Razao\s*Social|Prestador de)\s*[:.]?\s*', '', linha, flags=re.IGNORECASE).strip()
                    if len(candidato) > 3 and not re.search(r'(?:CNPJ|Inscrição|Endereço|Prefeitura|Município)', candidato, re.IGNORECASE):
                        fornecedor = candidato
                        break
                    # Se o nome estiver na linha imediatamente inferior
                    if idx + 1 < len(linhas_texto):
                        prox_linha = linhas_texto[idx + 1]
                        if len(prox_linha) > 3 and not re.search(r'(?:CNPJ|Inscrição|Endereço|Prefeitura|Município|Dados do)', prox_linha, re.IGNORECASE):
                            fornecedor = prox_linha
                            break

        # Se tudo falhar, pega a primeira linha corporativa limpa do topo (evitando nomes de órgãos públicos)
        if not fornecedor:
            for linha in linhas_texto[:10]:
                if len(linha) > 4 and not re.search(r'(?:PREFEITURA|MUNICÍPIO|NOTA\s*FISCAL|ELETRÔNICA|SECRETARIA|NFS-E|ISS|TRIBUTOS|ESTADO|FEDERAL|TOMADOR)', linha, re.IGNORECASE):
                    if not re.search(r'^(?:RUA|AV\.|AVENIDA|PRAÇA|PC\.)', linha, re.IGNORECASE) and not re.search(r'^\d', linha):
                        fornecedor = linha
                        break

        # Capturas padrão de Número, Valor e Data
        numero = ""
        padroes_numero = [
            r'(?:NÚMERO|NUMERO|Nº\s*DA\s*NOTA|Nº|Nota\s*Nº)\s*[:.]?\s*([0-9\.\-/]+)',
            r'(?:Nota\s*Fiscal\s*Eletrônica|NFS-e|NF-e)\s*[:.]?\s*([0-9\.\-/]+)'
        ]
        for padrao in padroes_numero:
            match = re.search(padrao, texto_completo, re.IGNORECASE)
            if match and match.group(1):
                num_candidato = limpar_numero_nota(match.group(1))
                if num_candidato and num_candidato != "Não identificado":
                    numero = num_candidato
                    break
        if not numero:
            numero = extrair_numero_do_nome_arquivo(nome_arquivo)

        valor = "0,00"
        valor_match = re.search(r'(?:VALOR TOTAL|VALOR LÍQUIDO|VALOR LIQUIDO|TOTAL DA NOTA|VALOR\s*DO\s*SERVIÇO|R\$)\s*[:.]?\s*([\d\.,]+)', texto_completo, re.IGNORECASE)
        if valor_match:
            valor = valor_match.group(1).strip().rstrip('.')
        else:
            todos_valores = re.findall(r'\b\d{1,3}(?:\.\d{3})*,\d{2}\b', texto_completo)
            if todos_valores:
                valor = todos_valores[-1]

        data_match = re.search(r'(\d{2}/\d{2}/\d{4})', texto_completo)
        data = data_match.group(1) if data_match else "Não encontrada"

        return {
            "Número da Nota": numero if numero else "Não identificado",
            "CNPJ": cnpj,
            "Valor": valor,
            "Data": data,
            "Fornecedor": limpar_nome_fornecedor(fornecedor) if fornecedor else "Verificar no PDF"
        }
    except Exception:
        return {
            "Número da Nota": extrair_numero_do_nome_arquivo(nome_arquivo) or "Erro de Leitura",
            "CNPJ": "Erro",
            "Valor": "0,00",
            "Data": "Erro",
            "Fornecedor": "Falha na análise estrutural"
        }

# --- PROCESSADOR INDIVIDUAL ---

def processar_arquivo(item):
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
