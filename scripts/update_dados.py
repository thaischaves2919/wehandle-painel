"""
Script de atualização automática do painel wehandle.
Roda via GitHub Actions 3x por dia (08h, 13h e 18h BRT).
Usa Metabase Card API com token de sessão SSO → atualiza dados.js → commit + push.
Só atualiza clientes cujo período de 45 dias ainda não encerrou.
Sincroniza automaticamente novos fornecedores dos clientes ativos.
"""
import os
import re
import json
import datetime
import requests

MB_URL     = "https://mbwh.wehandle.com.br"
MB_SESSION = os.environ["MB_SESSION"]
NTFY_TOPIC     = os.environ.get("NTFY_TOPIC", "")
ZAPI_INSTANCE  = os.environ.get("ZAPI_INSTANCE", "")
ZAPI_TOKEN     = os.environ.get("ZAPI_TOKEN", "")
TODAY      = datetime.date.today().isoformat()          # YYYY-MM-DD
TODAY_BR   = datetime.date.today().strftime("%d/%m/%Y") # DD/MM/YYYY

session = requests.Session()
session.headers.update({
    "Content-Type": "application/json",
    "X-Metabase-Session": MB_SESSION
})

# ── 1. Verificar autenticação ──────────────────────────────────────────────────
def autenticar():
    r = session.get(f"{MB_URL}/api/user/current")
    if r.status_code == 200:
        nome = r.json().get("first_name") or r.json().get("email", "usuário")
        print(f"✓ Autenticado no Metabase como {nome}")
    else:
        raise Exception(f"Token expirado (status {r.status_code}). Renove MB_SESSION em GitHub > Settings > Secrets.")

# ── 2. Executar card Metabase ──────────────────────────────────────────────────
def run_card(card_id, parameters=None):
    body = {"parameters": parameters or []}
    r = session.post(f"{MB_URL}/api/card/{card_id}/query", json=body, timeout=60)
    if not r.ok:
        print(f"  Erro {r.status_code} no card {card_id}")
        return []
    data = r.json().get("data", {})
    cols = [c["name"] for c in data.get("cols", [])]
    rows = data.get("rows", [])
    return [dict(zip(cols, row)) for row in rows]

# ── 3. Vidas (Card 7481 — Resultado Movimentação Período) ─────────────────────
def get_vidas(idempresa, data_inicio):
    params = [
        {"type": "number/=",    "target": ["variable", ["template-tag", "empresa"]],     "value": idempresa},
        {"type": "date/single", "target": ["variable", ["template-tag", "data_inicio"]], "value": data_inicio},
        {"type": "date/single", "target": ["variable", ["template-tag", "data_fim"]],    "value": TODAY},
    ]
    rows = run_card(7481, params)
    if rows:
        v = rows[0].get("total_periodo")
        if v is not None and int(v) > 0:
            return int(v)
    return None

# ── 4. Aderência ───────────────────────────────────────────────────────────────
def get_aderencia_netzsch():
    rows = run_card(30553)
    if rows:
        perc = rows[-1].get("PORCENTAGEM_ADERENTE")
        if perc is not None:
            return round(float(str(perc).replace(",", ".")) * 100, 2)
    return None

def get_aderencia_card_breakdown(card_id):
    rows = run_card(card_id)
    ad, na = 0, 0
    for row in rows:
        vals = list(row.values())
        categoria = str(vals[0]).lower()
        qtd = int(vals[1] or 0)
        if "não" in categoria or "nao" in categoria:
            na += qtd
        else:
            ad += qtd
    total = ad + na
    if total > 0:
        return round(ad / total * 100, 2)
    return None

def get_aderencia_por_sd(table_name):
    """Calcula aderência via SQL GROUP BY STATUSDOC — eficiente para qualquer tamanho de tabela."""
    sql = (
        f"SELECT STATUSDOC, COUNT(*) AS cnt "
        f"FROM DATABUNKER.{table_name} "
        f"WHERE STATUSDOC NOT IN (11, 12) "
        f"GROUP BY STATUSDOC"
    )
    body = {"database": 14, "type": "native", "native": {"query": sql}}
    r = session.post(f"{MB_URL}/api/dataset", json=body, timeout=60)
    if not r.ok:
        return None
    data = r.json().get("data", {})
    cols = [c["name"].upper() for c in data.get("cols", [])]
    rows = data.get("rows", [])
    if not rows:
        return None
    idx_s = next((i for i, c in enumerate(cols) if c == "STATUSDOC"), 0)
    idx_c = next((i for i, c in enumerate(cols) if c == "CNT"), 1)
    aderentes = sum(row[idx_c] for row in rows if row[idx_s] not in [0, 7])
    total     = sum(row[idx_c] for row in rows)
    if total > 0:
        return round(aderentes / total * 100, 2)
    return None

def get_aderencia_por_cards(card_ader, card_nao_ader):
    r_ad = run_card(card_ader)
    r_na = run_card(card_nao_ader)
    if r_ad and r_na:
        ad = int(r_ad[0].get("documentos") or r_ad[0].get(list(r_ad[0].keys())[0]) or 0)
        na = int(r_na[0].get("documentos") or r_na[0].get(list(r_na[0].keys())[0]) or 0)
        total = ad + na
        if total > 0:
            return round(ad / total * 100, 2)
    return None

# ── 5a. Métricas por fornecedor (PROD_ANALYTICS) ─────────────────────────────

def _run_native(sql):
    """Executa SQL nativo no Snowflake (database 14) — para tabelas DATABUNKER e PROD_ANALYTICS."""
    body = {"database": 14, "type": "native", "native": {"query": sql}}
    r = session.post(f"{MB_URL}/api/dataset", json=body, timeout=60)
    if not r.ok:
        return None
    data = r.json().get("data", {})
    cols = [c["name"].upper() for c in data.get("cols", [])]
    rows = data.get("rows", [])
    return [dict(zip(cols, row)) for row in rows]


def get_aderencia_por_fornecedor(idempresa):
    """Retorna {cnpj_digits: aderencia_pct} via GOLD_ADERENCIA (data mais recente)."""
    sql = f"""
    WITH ultima AS (
        SELECT MAX(DT_INSERTED) AS dt
        FROM PROD_ANALYTICS.GOLD.GOLD_ADERENCIA
        WHERE REF_ID_EMPRESA_TOMADOR_PRINCIPAL = '{idempresa}'
    )
    SELECT
        REGEXP_REPLACE(a.CD_CNPJ, '[^0-9]', '') AS CNPJ_NUM,
        CASE WHEN a.VL_TOTAL > 0
             THEN ROUND(a.VL_TOTAL_DOCUMENTOS_ADERENTE / a.VL_TOTAL * 100, 2)
             ELSE 0
        END AS ADERENCIA_PCT
    FROM PROD_ANALYTICS.GOLD.GOLD_ADERENCIA a
    JOIN ultima ON a.DT_INSERTED = ultima.dt
    WHERE a.REF_ID_EMPRESA_TOMADOR_PRINCIPAL = '{idempresa}'
    """
    rows = _run_native(sql)
    if not rows:
        return {}
    return {r["CNPJ_NUM"]: float(r["ADERENCIA_PCT"] or 0) for r in rows if r.get("CNPJ_NUM")}


def get_vidas_por_fornecedor(idempresa):
    """Retorna {nome_upper: total_vidas} do dia mais recente em GOLD_VIDAS_ALOCADAS_DIARIO."""
    sql = f"""
    SELECT UPPER(TRIM(NM_EMPRESA_FORNECEDOR)) AS NOME, SUM(QTD_VIDAS) AS VIDAS
    FROM PROD_ANALYTICS.GOLD.GOLD_VIDAS_ALOCADAS_DIARIO
    WHERE REF_ID_EMPRESA_TOMADOR_PRINCIPAL = '{idempresa}'
      AND DT_DIA_REFERENCIA = (
          SELECT MAX(DT_DIA_REFERENCIA) FROM PROD_ANALYTICS.GOLD.GOLD_VIDAS_ALOCADAS_DIARIO
          WHERE REF_ID_EMPRESA_TOMADOR_PRINCIPAL = '{idempresa}'
      )
    GROUP BY UPPER(TRIM(NM_EMPRESA_FORNECEDOR))
    HAVING SUM(QTD_VIDAS) > 0
    """
    rows = _run_native(sql)
    if not rows:
        return {}
    return {r["NOME"]: int(r["VIDAS"] or 0) for r in rows if r.get("NOME")}


def _match_nome_vidas(razao_social, vidas_map):
    """Casa nome do fornecedor (dados.js) com chave do vidas_map (case-insensitive, substring)."""
    nome = razao_social.upper().strip()
    if nome in vidas_map:
        return vidas_map[nome]
    for nome_gold, vidas in vidas_map.items():
        if nome_gold in nome or nome in nome_gold:
            return vidas
    return None


def atualizar_metricas_fornecedores(conteudo, cliente_id, aderencia_map, vidas_map):
    """Atualiza aderencia e vidas de cada fornecedor no bloco do cliente em dados.js."""
    idx_cli = conteudo.find(f"id: '{cliente_id}'")
    if idx_cli == -1:
        return conteudo, 0
    idx_fb = conteudo.find("fornecedores:", idx_cli)
    if idx_fb == -1:
        return conteudo, 0
    inicio = conteudo.find("[", idx_fb)
    # Delimitar pelo próximo bloco de cliente
    prox = re.search(r"\n    \{", conteudo[idx_fb + 200:])
    fim_busca = (idx_fb + 200 + prox.start()) if prox else len(conteudo)
    fim = conteudo.rfind("]", inicio, fim_busca)
    if inicio == -1 or fim == -1:
        return conteudo, 0

    bloco_orig = conteudo[inicio:fim + 1]
    bloco = bloco_orig
    atualizados = 0

    for m in re.finditer(r"\{[^{}]*cnpj:\s*'([^']+)'[^{}]*\}", bloco_orig):
        cnpj_fmt = m.group(1)
        cnpj_num = re.sub(r'\D', '', cnpj_fmt)
        linha = m.group(0)
        nova = linha

        # Aderência por CNPJ (confiável)
        if cnpj_num in aderencia_map:
            ader = aderencia_map[cnpj_num]
            if re.search(r'\baderencia:\s*[\d.]+', nova):
                nova = re.sub(r'\baderencia:\s*[\d.]+', f'aderencia: {ader}', nova)
            else:
                nova = re.sub(r'(\bvidas:\s*\d+)', rf'\1, aderencia: {ader}', nova)

        # Vidas por nome (melhor esforço)
        if vidas_map:
            m_nome = re.search(r"razaoSocial:\s*'([^']+)'", nova)
            if m_nome:
                vidas_val = _match_nome_vidas(m_nome.group(1), vidas_map)
                if vidas_val is not None:
                    nova = re.sub(r'\bvidas:\s*\d+', f'vidas: {vidas_val}', nova)

        if nova != linha:
            bloco = bloco.replace(linha, nova, 1)
            atualizados += 1

    conteudo = conteudo[:inicio] + bloco + conteudo[fim + 1:]
    return conteudo, atualizados


# ── 5. Fornecedores — busca e sincronização ────────────────────────────────────
def get_contatos_prestadores(idempresa):
    """Busca telefone e e-mail dos fornecedores via SILVER_VW_PRESTADORES_CONTATO (table 9046)."""
    body = {
        "database": 14,
        "type": "query",
        "query": {
            "source-table": 9046,
            "filter": ["=", ["field", 218559, None], idempresa],
            "limit": 1000
        }
    }
    r = session.post(f"{MB_URL}/api/dataset", json=body, timeout=60)
    if not r.ok:
        return {}
    data = r.json().get("data", {})
    cols = [c["name"] for c in data.get("cols", [])]
    rows = data.get("rows", [])
    idx_cnpj  = next((i for i, c in enumerate(cols) if c == "CNPJ"), None)
    idx_email = next((i for i, c in enumerate(cols) if c == "EMAIL"), None)
    idx_tel   = next((i for i, c in enumerate(cols) if c == "TELEFONE"), None)
    contatos = {}
    for row in rows:
        cnpj = re.sub(r'\D', '', row[idx_cnpj] or "") if idx_cnpj is not None else ""
        if cnpj and cnpj not in contatos:
            tel   = re.sub(r'\D', '', row[idx_tel]   or "") if idx_tel   is not None else ""
            email = (row[idx_email] or "").strip()          if idx_email is not None else ""
            contatos[cnpj] = {"tel": tel, "email": email}
    return contatos

def get_fornecedores_sd(table_id, idempresa=None):
    """Busca fornecedores únicos de uma tabela SD_CLIENTE, com telefone e e-mail via contatos."""
    body = {
        "database": 14,
        "type": "query",
        "query": {"source-table": table_id, "limit": 2000}
    }
    r = session.post(f"{MB_URL}/api/dataset", json=body, timeout=60)
    if not r.ok:
        print(f"  Erro {r.status_code} ao buscar fornecedores (table {table_id})")
        return []
    data = r.json().get("data", {})
    cols = [c["name"] for c in data.get("cols", [])]
    rows = data.get("rows", [])

    idx_nome  = next((i for i, c in enumerate(cols) if c == "NOMEEMPRESATERCEIRO"), None)
    idx_cnpj  = next((i for i, c in enumerate(cols) if c == "CNPJ"), None)
    idx_email = next((i for i, c in enumerate(cols) if c == "EMAILCONTRATADA"), None)

    if idx_cnpj is None:
        return []

    # Buscar contatos (tel + email) da tabela de prestadores
    contatos = get_contatos_prestadores(idempresa) if idempresa else {}

    vistos = {}
    for row in rows:
        cnpj  = row[idx_cnpj]  if idx_cnpj  is not None else ""
        nome  = row[idx_nome]  if idx_nome  is not None else ""
        email = row[idx_email] if idx_email is not None else ""
        if cnpj and cnpj not in vistos:
            cnpj_num = re.sub(r'\D', '', cnpj)
            contato = contatos.get(cnpj_num, {})
            vistos[cnpj] = {
                "razaoSocial": nome or cnpj,
                "cnpj": cnpj,
                "email": contato.get("email") or email or "",
                "tel":   contato.get("tel") or "",
            }
    return list(vistos.values())

def sincronizar_fornecedores(conteudo, cliente_id, fornecedores_metabase):
    """Adiciona ao dados.js os fornecedores novos do Metabase sem sobrescrever os existentes."""
    idx_cliente = conteudo.find(f"id: '{cliente_id}'")
    if idx_cliente == -1:
        return conteudo, 0

    # Encontrar o bloco de fornecedores deste cliente
    idx_forn = conteudo.find("fornecedores:", idx_cliente)
    if idx_forn == -1:
        return conteudo, 0
    inicio_arr = conteudo.find("[", idx_forn)
    fim_arr    = conteudo.rfind("]", inicio_arr, conteudo.find("\n    }", idx_forn + 200))
    if inicio_arr == -1 or fim_arr == -1:
        return conteudo, 0

    bloco = conteudo[inicio_arr:fim_arr + 1]

    # CNPJs já existentes no dados.js para este cliente
    cnpjs_existentes = set(re.findall(r"cnpj:\s*'([^']+)'", bloco))

    novos = []
    for f in fornecedores_metabase:
        cnpj_limpo = re.sub(r'\D', '', f["cnpj"])
        # Verificar se CNPJ já existe (comparando só dígitos)
        ja_existe = any(re.sub(r'\D', '', c) == cnpj_limpo for c in cnpjs_existentes)
        if not ja_existe and cnpj_limpo:
            cnpj_fmt = f["cnpj"]
            email = f.get("email", "").strip() or ""
            tel   = f.get("tel",   "").strip() or ""
            razao = (f.get("razaoSocial") or cnpj_fmt).strip()
            nova_linha = f"        {{ razaoSocial: '{razao}', cnpj: '{cnpj_fmt}', contrato: 'contratado', tel: '{tel}', email: '{email}', via: 'whatsapp', vidas: 0, status: 'pendente', dataEntrada: '{TODAY}' }}"
            novos.append(nova_linha)

    if not novos:
        return conteudo, []

    insercao = ",\n".join(novos)
    bloco_sem_trailing = re.sub(r',(\s*\])', r'\1', bloco)
    novo_bloco = bloco_sem_trailing[:-1].rstrip() + ",\n" + insercao + "\n      ]"

    conteudo = conteudo[:inicio_arr] + novo_bloco + conteudo[fim_arr + 1:]
    novos_objs = [f for f in fornecedores_metabase if re.sub(r'\D', '', f["cnpj"]) not in {re.sub(r'\D', '', c) for c in cnpjs_existentes} and re.sub(r'\D', '', f["cnpj"])]
    return conteudo, novos_objs

def atualizar_contatos_existentes(conteudo, cliente_id, contatos_mb):
    """Atualiza tel e e-mail de fornecedores já existentes no dados.js com dados do Metabase."""
    if not contatos_mb:
        return conteudo, 0

    idx_cliente = conteudo.find(f"id: '{cliente_id}'")
    if idx_cliente == -1:
        return conteudo, 0

    idx_forn = conteudo.find("fornecedores:", idx_cliente)
    if idx_forn == -1:
        return conteudo, 0
    inicio_arr = conteudo.find("[", idx_forn)
    fim_arr    = conteudo.rfind("]", inicio_arr, conteudo.find("\n    }", idx_forn + 200))
    if inicio_arr == -1 or fim_arr == -1:
        return conteudo, 0

    bloco    = conteudo[inicio_arr:fim_arr + 1]
    atualizados = 0

    for cnpj_num, dados in contatos_mb.items():
        tel_mb   = dados.get("tel",   "").strip()
        email_mb = dados.get("email", "").strip()
        if not tel_mb and not email_mb:
            continue

        # Localiza a linha do fornecedor pelo CNPJ (formatos variados)
        padrao_cnpj = re.compile(r"cnpj:\s*'([^']*" + re.escape(cnpj_num[-8:]) + r"[^']*)'")
        m = padrao_cnpj.search(bloco)
        if not m:
            continue

        linha_inicio = bloco.rfind("{", 0, m.start())
        linha_fim    = bloco.find("}", m.end()) + 1
        if linha_inicio == -1 or linha_fim == 0:
            continue
        linha = bloco[linha_inicio:linha_fim]
        nova_linha = linha

        if tel_mb:
            nova_linha = re.sub(r"tel:\s*''", f"tel: '{tel_mb}'", nova_linha)
            nova_linha = re.sub(r"(tel:\s*')([^']+)(')", lambda x: x.group(0) if x.group(2) else f"tel: '{tel_mb}'", nova_linha)
            # Só atualiza tel vazio
            nova_linha = re.sub(r"tel:\s*''", f"tel: '{tel_mb}'", nova_linha)

        if email_mb:
            nova_linha = re.sub(r"email:\s*''", f"email: '{email_mb}'", nova_linha)

        if nova_linha != linha:
            bloco = bloco[:linha_inicio] + nova_linha + bloco[linha_fim:]
            atualizados += 1

    conteudo = conteudo[:inicio_arr] + bloco + conteudo[fim_arr + 1:]
    return conteudo, atualizados

# ── 6. Ler e atualizar dados.js ────────────────────────────────────────────────
DADOS_PATH = "dados.js"

def ler_dados():
    with open(DADOS_PATH, encoding="utf-8") as f:
        return f.read()

def salvar_dados(conteudo):
    with open(DADOS_PATH, "w", encoding="utf-8") as f:
        f.write(conteudo)

def atualizar_campo(conteudo, cliente_id, campo, valor):
    idx = conteudo.find(f"id: '{cliente_id}'")
    if idx == -1:
        return conteudo
    padrao = re.compile(rf"({re.escape(campo)}:\s*)([^,\n]+)")
    match = padrao.search(conteudo, idx)
    if match:
        conteudo = conteudo[:match.start()] + f"{match.group(1)}{valor}" + conteudo[match.end():]
    return conteudo

def atualizar_data_comentario(conteudo, data_br):
    return re.sub(r"// Última atualização:.*", f"// Última atualização: {data_br}", conteudo)

def bumpar_versao(conteudo):
    """Gera nova versão com data + sufixo incremental para forçar auto-refresh no painel."""
    sufixos = list("abcdefghijklmnopqrstuvwxyz")
    hoje = TODAY.replace("-", "")
    padrao = re.compile(r"versao:\s*'(\d{8})([a-z])'")
    m = padrao.search(conteudo)
    if m:
        data_atual, letra_atual = m.group(1), m.group(2)
        if data_atual == hoje:
            prox = sufixos[sufixos.index(letra_atual) + 1] if letra_atual != 'z' else 'z'
        else:
            prox = 'a'
        nova_versao = f"{hoje}{prox}"
    else:
        nova_versao = f"{hoje}a"
    return re.sub(r"versao:\s*'[^']+'", f"versao: '{nova_versao}'", conteudo)

def atualizar_historico(conteudo, cliente_id, vidas, aderencia):
    idx_cliente = conteudo.find(f"id: '{cliente_id}'")
    if idx_cliente == -1:
        return conteudo
    idx_hist = conteudo.find("historico:", idx_cliente)
    if idx_hist == -1:
        return conteudo
    entrada = f"{{ data: '{TODAY}', vidas: {vidas}, aderencia: {aderencia} }}"
    padrao_hoje = re.compile(rf"\{{\s*data:\s*'{re.escape(TODAY)}'[^}}]*\}}")
    inicio_arr = conteudo.find("[", idx_hist)
    fim_arr    = conteudo.find("]", inicio_arr)
    if inicio_arr == -1 or fim_arr == -1:
        return conteudo
    bloco = conteudo[inicio_arr:fim_arr + 1]
    if padrao_hoje.search(bloco):
        bloco_novo = padrao_hoje.sub(entrada, bloco)
    else:
        bloco_novo = bloco[:-1].rstrip().rstrip(',') + ",\n        " + entrada + "\n      ]"
    return conteudo[:inicio_arr] + bloco_novo + conteudo[fim_arr + 1:]

# ── 7. Verificar prazo de 45 dias ─────────────────────────────────────────────
def dentro_do_prazo(data_inicio_str):
    data_inicio = datetime.date.fromisoformat(data_inicio_str)
    prazo = data_inicio + datetime.timedelta(days=45)
    return datetime.date.today() <= prazo

# ── 8. Busca de telefone via CNPJ (BrasilAPI) ────────────────────────────────
def buscar_telefone_cnpj(cnpj):
    cnpj_num = re.sub(r'\D', '', cnpj)
    if len(cnpj_num) != 14:
        return ""
    try:
        r = requests.get(f"https://brasilapi.com.br/api/cnpj/v1/{cnpj_num}", timeout=10)
        if not r.ok:
            return ""
        data = r.json()
        tel = re.sub(r'\D', '', (data.get("ddd_telefone_1") or ""))
        if not tel:
            tel = re.sub(r'\D', '', (data.get("ddd_telefone_2") or ""))
        return tel
    except Exception:
        return ""

# ── 10. WhatsApp via Z-API ────────────────────────────────────────────────────
def formatar_telefone(tel):
    nums = re.sub(r'\D', '', tel)
    if not nums:
        return ""
    if nums.startswith("0"):
        nums = nums[1:]
    if not nums.startswith("55"):
        nums = "55" + nums
    return nums if 12 <= len(nums) <= 13 else ""

def enviar_whatsapp(telefone, razao_social, nome_cliente):
    if not ZAPI_INSTANCE or not ZAPI_TOKEN:
        return
    tel_fmt = formatar_telefone(telefone)
    if not tel_fmt:
        print(f"  ⚠ WhatsApp ignorado — telefone inválido: '{telefone}' ({razao_social})")
        return
    mensagem = (
        f"Olá, tudo bem?\n\n"
        f"Meu nome é Thais e sou responsável pela operação e pelo engajamento dos fornecedores "
        f"na plataforma da wehandle.\n\n"
        f"Estou entrando em contato para apoiar vocês neste início com a *{nome_cliente}*, "
        f"auxiliando no cadastro de colaboradores, aderência e conformidade da documentação "
        f"e explicando o funcionamento da plataforma e orientando da melhor forma possível."
    )
    try:
        url = f"https://api.z-api.io/instances/{ZAPI_INSTANCE}/token/{ZAPI_TOKEN}/send-text"
        r = requests.post(url, json={"phone": tel_fmt, "message": mensagem}, timeout=15)
        if r.ok:
            print(f"  ✓ WhatsApp enviado → {tel_fmt} ({razao_social})")
        else:
            print(f"  ⚠ WhatsApp falhou ({r.status_code}) → {razao_social}: {r.text}")
    except Exception as e:
        print(f"  ⚠ WhatsApp erro: {e}")

# ── 9. Notificação via ntfy.sh ────────────────────────────────────────────────
def enviar_notificacao(titulo, mensagem):
    if not NTFY_TOPIC:
        print("  ⚠ NTFY_TOPIC não configurado — notificação ignorada")
        return
    try:
        r = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=mensagem.encode("utf-8"),
            headers={
                "Title": titulo,
                "Priority": "default",
                "Tags": "bell,office",
            },
            timeout=10,
        )
        if r.status_code == 200:
            print(f"  ✓ Notificação enviada → ntfy.sh/{NTFY_TOPIC}")
        else:
            print(f"  ⚠ Notificação falhou — HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"  ⚠ Notificação falhou: {e}")

# ── 10. Relatório ──────────────────────────────────────────────────────────────
def formatar_cliente(nome, prazo, vidas, vidas_meta, aderencia, aderencia_meta, atualizado, novos_forn=0):
    if not atualizado:
        return f"\n{nome} (prazo: {prazo})\n  (sem atualização hoje)"
    falta_vidas = (vidas_meta - vidas) if vidas_meta and vidas else None
    gap_ader    = round(aderencia - aderencia_meta, 2) if aderencia_meta and aderencia else None
    vidas_str = f"Vidas: {vidas} | Meta F1: {vidas_meta} | " + (
        "✅" if falta_vidas is not None and falta_vidas <= 0 else f"Faltam: {falta_vidas}"
    ) if vidas else "Vidas: — (sem dados)"
    ader_str = f"Aderência: {aderencia}% | Meta: {aderencia_meta}% | " + (
        "✅" if gap_ader is not None and gap_ader >= 0 else f"Gap: {gap_ader} p.p."
    ) if aderencia else "Aderência: — (sem dados)"
    forn_str = f"\n  +{novos_forn} fornecedor(es) novo(s) adicionado(s)" if novos_forn > 0 else ""
    return f"\n{nome} (prazo: {prazo})\n  {vidas_str}\n  {ader_str}{forn_str}"

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    autenticar()

    clientes = [
        {
            "id": "netzsch", "nome": "Netzsch", "prazo": "06/06",
            "idempresa": 34863, "data_inicio": "2026-04-22",
            "metaVidasF1": 151, "metaAderencia": 50,
            "get_aderencia": lambda: get_aderencia_por_sd("SD_NETZSCH"),
            "sd_table_id": 10400,  # SD_NETZSCH
        },
        {
            "id": "saint-gobain", "nome": "Saint-Gobain", "prazo": "06/06",
            "idempresa": 6, "data_inicio": "2026-04-22",
            "metaVidasF1": 201, "metaAderencia": 50,
            "get_aderencia": lambda: get_aderencia_por_cards(31360, 31363),  # aguardando SD table_id
            "sd_table_id": None,  # período encerrado
        },
        {
            "id": "gestamp", "nome": "Gestamp", "prazo": "05/07",
            "idempresa": 77911, "data_inicio": "2026-05-21",
            "metaVidasF1": 201, "metaAderencia": 50,
            "get_aderencia": lambda: get_aderencia_por_sd("SD_GESTAMP"),
            "sd_table_id": 12220,  # SD_GESTAMP
        },
        {
            "id": "grupo-zelo", "nome": "Grupo Zelo", "prazo": "24/07",
            "idempresa": 78024, "data_inicio": "2026-06-09",
            "metaVidasF1": None, "metaAderencia": 50,
            "get_aderencia": lambda: get_aderencia_por_sd("SD_ZELO"),
            "sd_table_id": 12586,  # SD_ZELO
        },
        {
            "id": "melitta", "nome": "Melitta", "prazo": "24/07",
            "idempresa": 79099, "data_inicio": "2026-06-09",
            "metaVidasF1": 201, "metaAderencia": 50,
            "get_aderencia": lambda: get_aderencia_por_sd("SD_CELUPA"),
            "sd_table_id": 12905,  # SD_CELUPA
        },
    ]

    conteudo = ler_dados()
    relatorio_partes = [f"📊 Relatório diário — {TODAY_BR}\n"]
    novos_por_cliente = []   # lista de (nome, qtd) para notificação
    alguma_atualizacao = False  # controla se versão precisa bumpar

    for c in clientes:
        print(f"\n--- {c['nome']} ---")
        atualizado = False
        vidas     = get_vidas(c["idempresa"], c["data_inicio"])
        aderencia = c["get_aderencia"]()
        print(f"  Vidas: {vidas} | Aderência: {aderencia}%")

        # Sempre atualiza vidas/aderencia — equipe vê dados ao vivo
        if vidas and vidas > 0:
            conteudo = atualizar_campo(conteudo, c["id"], "vidas", vidas)
            atualizado = True
        if aderencia and aderencia > 0:
            conteudo = atualizar_campo(conteudo, c["id"], "aderencia", aderencia)
            atualizado = True

        # vidasFinal/aderenciaFinal só atualizam dentro do período — ficam congelados depois
        if dentro_do_prazo(c["data_inicio"]):
            if vidas and vidas > 0:
                conteudo = atualizar_campo(conteudo, c["id"], "vidasFinal", vidas)
            if aderencia and aderencia > 0:
                conteudo = atualizar_campo(conteudo, c["id"], "aderenciaFinal", aderencia)
            if atualizado and vidas and aderencia:
                conteudo = atualizar_historico(conteudo, c["id"], vidas, aderencia)
        else:
            print(f"  ⏸ Período encerrado — vidasFinal/aderenciaFinal congelados")

        # Sincronizar fornecedores novos e atualizar contatos existentes
        novos_forn = 0
        if c.get("sd_table_id"):
            print(f"  Buscando fornecedores na tabela {c['sd_table_id']}...")
            fornecedores_mb = get_fornecedores_sd(c["sd_table_id"], idempresa=c["idempresa"])

            # Atualiza tel/email de fornecedores já cadastrados
            contatos_mb = get_contatos_prestadores(c["idempresa"])
            conteudo, n_cont = atualizar_contatos_existentes(conteudo, c["id"], contatos_mb)
            if n_cont > 0:
                print(f"  ✓ {n_cont} contato(s) atualizado(s) do Metabase")
                atualizado = True

            conteudo, novos_lista = sincronizar_fornecedores(conteudo, c["id"], fornecedores_mb)
            novos_forn = len(novos_lista)
            if novos_forn > 0:
                print(f"  +{novos_forn} fornecedor(es) novo(s) adicionado(s)")
                atualizado = True
                novos_por_cliente.append((c["nome"], novos_forn))
                # Para novos fornecedores sem telefone, buscar via CNPJ
                for f in novos_lista:
                    if not f.get("tel"):
                        tel_cnpj = buscar_telefone_cnpj(f["cnpj"])
                        if tel_cnpj:
                            print(f"  ✓ Telefone encontrado via CNPJ: {tel_cnpj} ({f['razaoSocial']})")
                            cnpj_fmt = f["cnpj"]
                            conteudo = re.sub(
                                rf"(cnpj:\s*'{re.escape(cnpj_fmt)}'[^}}]*tel:\s*')(')",
                                rf"\g<1>{tel_cnpj}\2",
                                conteudo
                            )

        # Atualizar aderência e vidas por fornecedor (PROD_ANALYTICS)
        try:
            aderencia_forn = get_aderencia_por_fornecedor(c["idempresa"])
            vidas_forn     = get_vidas_por_fornecedor(c["idempresa"])
            if aderencia_forn or vidas_forn:
                conteudo, n_forn = atualizar_metricas_fornecedores(conteudo, c["id"], aderencia_forn, vidas_forn)
                if n_forn > 0:
                    print(f"  ✓ {n_forn} fornecedor(es) com aderência/vidas atualizados")
                    atualizado = True
        except Exception as e:
            print(f"  ⚠ Métricas por fornecedor falharam: {e}")

        if atualizado:
            alguma_atualizacao = True

        relatorio_partes.append(formatar_cliente(
            nome=f"🔵 {c['nome']}", prazo=c["prazo"],
            vidas=vidas, vidas_meta=c["metaVidasF1"],
            aderencia=aderencia, aderencia_meta=c["metaAderencia"],
            atualizado=atualizado, novos_forn=novos_forn,
        ))

    if alguma_atualizacao:
        conteudo = bumpar_versao(conteudo)

    conteudo = atualizar_data_comentario(conteudo, TODAY_BR)
    salvar_dados(conteudo)
    print(f"\n✓ dados.js atualizado — {TODAY_BR}")

    relatorio = "\n".join(relatorio_partes)
    print("\n" + relatorio)
    with open("relatorio.txt", "w", encoding="utf-8") as f:
        f.write(relatorio)

    # Notificação quando há novos fornecedores
    if novos_por_cliente:
        linhas = [f"• {nome}: +{qtd} fornecedor(es)" for nome, qtd in novos_por_cliente]
        total  = sum(q for _, q in novos_por_cliente)
        mensagem = (
            f"Atualização {TODAY_BR}\n"
            + "\n".join(linhas)
            + f"\n\nTotal: {total} novo(s) pendente(s) de contato."
        )
        enviar_notificacao("wehandle — Novos fornecedores detectados", mensagem)

if __name__ == "__main__":
    main()
