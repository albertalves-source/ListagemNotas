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
    """Limpa o número da nota removendo anos embutidos e zeros excessivos."""
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
    """Busca o número da nota baseado puramente no nome do arquivo."""
    nome_limpo = os.path.splitext(nome_arquivo)[0]
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

def eh_nome_valido(texto):
    """Valida se o texto extraído é realmente o nome de um fornecedor ou se é lixo/título da nota."""
    if not texto or len(texto.strip()) < 3:
        return False
    
    # Palavras que indicam títulos da nota ou órgãos públicos (não são o fornecedor)
    palavras_proibidas = [
        "PREFEITURA", "MUNICÍPIO", "MUNICIPIO", "SECRETARIA", "ESTADO", "FEDERAL", 
        "NOTA FISCAL", "NFS-E", "NF-E", "ELETRÔNICA", "ELETRONICA", "DIRETORIA",
        "PRESTADOR", "TOMADOR", "SERVIÇO", "SERVICO", "DADOS DO", "IDENTIFICAÇÃO",
        "EMITENTE", "DESTINATÁRIO", "DE SERVI", "DA NFS", "NOME / RAZÃO SOCIAL",
        "RAZÃO SOCIAL", "RAZAO SOCIAL", "NOME FANTASIA", "INSCRICAO", "INSCRIÇÃO",
        "CADASTRO", "CONTRIBUINTE", "ASSINATURA", "VALIDADE", "AUTENTICIDADE"
    ]
    
    texto_upper = texto.upper().strip()
    for palavra in palavras_proibidas:
        if palavra in texto_upper:
            return False
            
    return True

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
        
        fornecedor = buscar_tag(['xFant', 'nomeFantasia'])
        if not fornecedor or not eh_nome_valido(fornecedor):
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
            "Fornecedor": fornecedor if fornecedor else "Não identificado"
        }
    except Exception:
        return None

def extrair_dados_pdf(conteudo_bytes, nome_arquivo):
    """Extrai dados de um PDF usando pdfplumber com filtragem avançada anti-falsos positivos."""
    try:
        texto = ""
        linhas = []
        with pdfplumber.open(io.BytesIO(conteudo_bytes)) as pdf:
            for pagina in pdf.pages:
                texto_pag = pagina.extract_text()
                if texto_pag:
                    texto += texto_pag + "\n"
                    linhas.extend([l.strip() for l in texto_pag.split('\n') if l.strip()])

        if not texto.strip():
            return {
                "Número da Nota": extrair_numero_do_nome_arquivo(nome_arquivo) or "PDF Sem Texto",
                "CNPJ": "Requer OCR",
                "Valor": "0,00",
                "Data": "Requer OCR",
                "Fornecedor": "Imagem Escaneada"
            }

        # 1. Captura do Número da Nota
        numero = ""
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
        valor_match = re.search(r'(?:VALOR TOTAL|VALOR LÍQUIDO|VALOR LIQUIDO|TOTAL DA NOTA|VALOR\s*DO\s*SERVIÇO|R\$)\s*[:.]?\s*([\d\.,]+)', texto, re.IGNORECASE)
        if valor_match:
            valor = valor_match.group(1).strip().rstrip('.')
        else:
            todos_valores = re.findall(r'\b\d{1,3}(?:\.\d{3})*,\d{2}\b', texto)
            if todos_valores:
                valor = todos_valores[-1]

        # 4. Captura de Data
        data_match = re.search(r'(\d{2}/\d{2}/\d{4})', texto)
        data = data_match.group(1) if data_match else "Não encontrada"

        # 5. Captura Inteligente e Filtrada do Fornecedor (Foco em eliminar lixos estruturais)
        fornecedor = ""
        
        # Estratégia A: Varredura por proximidade de marcadores de Nome/Razão Social/Fantasia
        for idx, linha in enumerate(linhas):
            if re.search(r'(?:Nome Fantasia|Nome Comercial|Razão Social|Razao Social|Prestador|Emitente|Nome\s*/\s*Razão\s*Social)', linha, re.IGNORECASE):
                # Testa se o nome válido está na própria linha (pós-separador)
                candidato_linha = re.sub(r'^.*(?:Nome Fantasia|Nome Comercial|Razão Social|Razao Social|Prestador|Emitente|Nome\s*/\s*Razão\s*Social)\s*[:.]?\s*', '', linha, flags=re.IGNORECASE).strip()
                if eh_nome_valido(candidato_linha):
                    fornecedor = candidato_linha
                    break
                
                # Se não estava na mesma linha, verifica as 2 linhas subsequentes (comum em quebras de tabela)
                for k in range(1, 3):
                    if idx + k < len(linhas):
                        candidato_vizinho = linhas[idx + k]
                        if eh_nome_valido(candidato_vizinho):
                            fornecedor = candidato_vizinho
                            break
                if fornecedor:
                    break

        # Estratégia B: Se a estratégia A falhou ou trouxe lixo, busca a primeira linha corporativa válida fora do cabeçalho público
        if not fornecedor or not eh_nome_valido(fornecedor):
            for linha in linhas[:12]: # Varre o topo da nota
                if eh_nome_valido(linha) and len(linha) > 5:
                    # Evita linhas que começam com números soltos ou endereços óbvios
                    if not re.search(r'^(?:RUA|AV\.|AVENIDA|PRAÇA|PC\.)', linha, re.IGNORECASE) and not re.search(r'^\d', linha):
                        fornecedor = linha
                        break

        # Limpeza final cirúrgica para remover dados residuais na mesma linha
        if fornecedor:
            fornecedor = re.sub(r'\s*(?:CNPJ|Inscrição|CPF|E-mail|Fone|Telefone|Endereço|Bairro|CEP|Cidade|UF).*$', '', fornecedor, flags=re.IGNORECASE).strip()
            # Remove pontuações soltas no fim do nome
            fornecedor = fornecedor.rstrip(':-. ')
            
        if not fornecedor or not eh_nome_valido(fornecedor):
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
            "Fornecedor": "Falha no processador"
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
