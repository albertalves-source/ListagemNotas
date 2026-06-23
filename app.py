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

def limpar_numero_nota(num_str):
    """Limpa o número da nota removendo anos embutidos e zeros excessivos (ex: 20260000000032 -> 32)."""
    if not num_str:
        return ""
    num_limpo = re.sub(r'\D', '', num_str) # Mantém apenas dígitos
    if not num_limpo:
        return ""
    
    # Se o número for muito longo (comum em chaves ou numeração composta de prefeituras)
    if len(num_limpo) > 9:
        # Se começa com o ano atual ou recente (ex: 2026... ou 2025...) elimina o prefixo do ano e zeros
        match = re.search(r'^(?:2023|2024|2025|2026|2027)0*([1-9]\d*)$', num_limpo)
        if match:
            return match.group(1)
        # Caso contrário, apenas remove os zeros iniciais do bloco final
        num_limpo = num_limpo.lstrip('0')
    
    return num_limpo if num_limpo else "Não identificado"

def extrair_numero_do_nome_arquivo(nome_arquivo):
    """Busca o número da nota baseado puramente no nome do arquivo."""
    nome_limpo = os.path.splitext(nome_arquivo)[0]
    # Padrão para nomes como "100.pdf", "NF 42.pdf", "Nota_123.pdf"
    match = re.search(r'(?:NF|NF-e|NFS-e|NOTA|Nº)?\s*[_ \-]*([1-9]\d{0,8})\b', nome_limpo, re.IGNORECASE)
    if match:
        return match.group(1)
    return ""

def formatar_cnpj(cnpj_str):
    """Garante que o CNPJ retornado esteja limpo e legível."""
    if not cnpj_str:
        return "Não encontrado"
    cnpj_limpo = re.sub(r'\D', '', cnpj_str)
    if len(cnpj_limpo) == 14:
        return f"{cnpj_limpo[:2]}.{cnpj_limpo[2:5]}.{cnpj_limpo[5:8]}/{cnpj_limpo[8:12]}-{cnpj_limpo[12:]}"
    return cnpj_str

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
        cnpj = buscar_tag(['CNPJ', 'Cnpj', 'cnpjPrestador', 'cnpjEmitente'])
        fornecedor = buscar_tag(['xNome', 'RazaoSocial', 'nomePrestador', 'nomeEmitente', 'xFant'])
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
            "Fornecedor": fornecedor if fornecedor else "Não identificado"
        }
    except Exception:
        return None

def extrair_dados_pdf(conteudo_bytes, nome_arquivo):
    """Extrai dados de um PDF usando heurística avançada para notas municipais/estaduais."""
    try:
        pdf = PdfReader(io.BytesIO(conteudo_bytes))
        texto = ""
        for page in pdf.pages:
            texto += page.extract_text() or ""
            
        if not texto.strip():
            return {
                "Número da Nota": extrair_numero_do_nome_arquivo(nome_arquivo) or "PDF Sem Texto",
                "CNPJ": "Requer OCR / Imagem",
                "Valor": "0,00",
                "Data": "Requer OCR",
                "Fornecedor": "PDF Escaneado"
            }

        # 1. Captura do Número da Nota
        numero = ""
        # Procura padrões estruturados primeiro
        padroes_numero = [
            r'(?:NÚMERO|NUMERO|Nº\s*DA\s*NOTA|Nº|Nota\s*Nº)\s*[:.]?\s*([0-9\.\-/]+)',
            r'(?:Nota\s*Fiscal\s*Eletrônica|NFS-e|NF-e)\s*[:.]?\s*([0-9\.\-/]+)'
        ]
        for padrao in padroes_numero:
            match = re.search(padrao, texto, re.IGNORECASE)
            if match and match.group(1):
                num_candidato = limpar_numero_nota(match.group(1))
                if num_candidato and num_candidato != "Não identificado":
                    numero = num_candidato
                    break
        
        if not numero:
            numero = extrair_numero_do_nome_arquivo(nome_arquivo)

        # 2. Captura de CNPJ
        cnpj_match = re.search(r'(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}|\d{14})', texto)
        cnpj = formatar_cnpj(cnpj_match.group(1)) if cnpj_match else "Não encontrado"

        # 3. Captura do Valor da Nota
        valor = "0,00"
        # Procura palavras-chave fortes seguidas por valores monetários formato BR (ex: 2.485.739,00 ou 933,33)
        valor_match = re.search(r'(?:VALOR TOTAL|VALOR LÍQUIDO|VALOR LIQUIDO|TOTAL DA NOTA|VALOR\s*DO\s*SERVIÇO|R\$)\s*[:.]?\s*([\d\.,]+)', texto, re.IGNORECASE)
        if valor_match:
            valor = valor_match.group(1).strip().rstrip('.')
        else:
            # Fallback secundário: pega o último padrão de valor financeiro que aparece na nota (geralmente o totalizador inferior)
            todos_valores = re.findall(r'\b\d{1,3}(?:\.\d{3})*,\d{2}\b', texto)
            if todos_valores:
                valor = todos_valores[-1]

        # 4. Captura de Data
        data_match = re.search(r'(\d{2}/\d{2}/\d{4})', texto)
        data = data_match.group(1) if data_match else "Não encontrada"

        # 5. Captura do Fornecedor (Foco em mitigar falsos positivos como "DA NFS", "DE SERVI")
        fornecedor = ""
        # Captura texto associado diretamente aos marcadores de Prestador/Emitente
        linhas = [l.strip() for l in texto.split('\n') if l.strip()]
        
        for idx, linha in enumerate(linhas):
            if re.search(r'(?:Razão Social|Razao Social|Prestador|Emitente|Nome\s*/\s*Razão\s*Social)', linha, re.IGNORECASE):
                # Se a palavra chave está sozinha ou quase sozinha, o nome está na linha de baixo
                if len(linha) < 25 and idx + 1 < len(linhas):
                    candidato = linhas[idx + 1]
                else:
                    candidato = re.sub(r'(?:Razão Social|Razao Social|Prestador|Emitente|Nome\s*/\s*Razão\s*Social)\s*[:.]?\s*', '', linha, flags=re.IGNORECASE)
                
                # Validação para não pegar títulos estruturais do formulário
                if candidato and not re.search(r'(?:CNPJ|Inscrição|Endereço|Bairro|Cidade|UF|CEP|Telefone|Dados do|Documento|NFS-e|Nota)', candidato, re.IGNORECASE):
                    if len(candidato.strip()) > 3:
                        fornecedor = candidato.strip()
                        break

        # Limpeza fina para remover restos de labels capturados por acidente
        if fornecedor:
            fornecedor = re.sub(r'\s*(?:CNPJ|Inscrição|CPF|E-mail|Fone|Telefone).*$', '', fornecedor, flags=re.IGNORECASE).strip()
            # Remove termos puramente genéricos que restaram
            if fornecedor.upper() in ["PRESTADOR DE SERVIÇOS", "DADOS DO PRESTADOR", "EMITENTE", "NOME / RAZÃO SOCIAL", "DE SERVI", "DA NFS"]:
                fornecedor = ""

        if not fornecedor:
            # Caso os filtros falhem, tenta localizar a primeira linha corporativa do topo da nota
            for linha in linhas[:8]:
                if len(linha) > 4 and not re.search(r'(?:PREFEITURA|MUNICÍPIO|NOTA FISCAL|ELETRÔNICA|SECRETARIA|NFS-e|ISS|TRIBUTOS)', linha, re.IGNORECASE):
                    fornecedor = linha
                    break
            if not fornecedor:
                fornecedor = "Verificar no PDF"

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
            "Fornecedor": "Falha no mapeamento estrutural"
        }

# --- PROCESSADOR INDIVIDUAL ---

def processar_arquivo(item):
    """Processa o arquivo e retorna uma linha estruturada garantida."""
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
            
            # Estrutura limpa de acordo com a sua solicitação
            colunas_ordem = ["Número da Nota", "CNPJ", "Valor", "Data", "Fornecedor", "Arquivo"]
            for col in colunas_ordem:
                if col not in df.columns:
                    df[col] = ""
            df = df[colunas_ordem]
            
            st.success(f"Sucesso! {len(df)} de {total_arquivos} notas processadas.")
            
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
