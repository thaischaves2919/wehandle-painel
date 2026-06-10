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
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
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
        v = rows[0].get("final_periodo")
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
            nova_linha = f"        {{ razaoSocial: '{razao}', cnpj: '{cnpj_fmt}', contrato: 'contratado', tel: '{tel}', email: '{email}', via: 'whatsapp', vidas: 0, status: 'pendente' }}"
            novos.append(nova_linha)

    if not novos:
        return conteudo, 0

    insercao = ",\n".join(novos)
    # Remover vírgulas finais extras antes de inserir
    bloco_sem_trailing = re.sub(r',(\s*\])', r'\1', bloco)
    novo_bloco = bloco_sem_trailing[:-1].rstrip() + ",\n" + insercao + "\n      ]"

    conteudo = conteudo[:inicio_arr] + novo_bloco + conteudo[fim_arr + 1:]
    return conteudo, len(novos)

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
        bloco_novo = bloco[:-1].rstrip() + ",\n        " + entrada + "\n      ]"
    return conteudo[:inicio_arr] + bloco_novo + conteudo[fim_arr + 1:]

# ── 7. Verificar prazo de 45 dias ─────────────────────────────────────────────
def dentro_do_prazo(data_inicio_str):
    data_inicio = datetime.date.fromisoformat(data_inicio_str)
    prazo = data_inicio + datetime.timedelta(days=45)
    return datetime.date.today() <= prazo

# ── 8. Notificação via ntfy.sh ────────────────────────────────────────────────
def enviar_notificacao(titulo, mensagem):
    if not NTFY_TOPIC:
        return
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=mensagem.encode("utf-8"),
            headers={
                "Title": titulo,
                "Priority": "default",
                "Tags": "bell,office",
            },
            timeout=10,
        )
        print(f"  ✓ Notificação enviada → ntfy.sh/{NTFY_TOPIC}")
    except Exception as e:
        print(f"  ⚠ Notificação falhou: {e}")

# ── 9. Relatório ───────────────────────────────────────────────────────────────
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
            "get_aderencia": lambda: get_aderencia_netzsch(),
            "sd_table_id": None,  # período encerrado
        },
        {
            "id": "saint-gobain", "nome": "Saint-Gobain", "prazo": "06/06",
            "idempresa": 6, "data_inicio": "2026-04-22",
            "metaVidasF1": 201, "metaAderencia": 50,
            "get_aderencia": lambda: get_aderencia_por_cards(31360, 31363),
            "sd_table_id": None,  # período encerrado
        },
        {
            "id": "gestamp", "nome": "Gestamp", "prazo": "05/07",
            "idempresa": 77911, "data_inicio": "2026-05-21",
            "metaVidasF1": 201, "metaAderencia": 50,
            "get_aderencia": lambda: get_aderencia_por_cards(38145, 38154),
            "sd_table_id": 12220,  # SD_GESTAMP
        },
        {
            "id": "grupo-zelo", "nome": "Grupo Zelo", "prazo": "24/07",
            "idempresa": 78024, "data_inicio": "2026-06-09",
            "metaVidasF1": None, "metaAderencia": 50,
            "get_aderencia": lambda: get_aderencia_card_breakdown(39958),
            "sd_table_id": 12586,  # SD_ZELO
        },
        # Melitta — aguardando idempresa e cards de aderência (ainda não cadastrada no Metabase)
        # {
        #     "id": "melitta", "nome": "Melitta", "prazo": "24/07",
        #     "idempresa": ???, "data_inicio": "2026-06-09",
        #     "metaVidasF1": None, "metaAderencia": 50,
        #     "get_aderencia": lambda: get_aderencia_por_cards(???, ???),
        #     "sd_table_id": ???,
        # },
    ]

    conteudo = ler_dados()
    relatorio_partes = [f"📊 Relatório diário — {TODAY_BR}\n"]
    novos_por_cliente = []   # lista de (nome, qtd) para notificação

    for c in clientes:
        if not dentro_do_prazo(c["data_inicio"]):
            print(f"\n--- {c['nome']} --- (período encerrado, pulando)")
            relatorio_partes.append(f"\n⚪ {c['nome']} (prazo: {c['prazo']})\n  (período encerrado — sem atualização)")
            continue

        print(f"\n--- {c['nome']} ---")
        vidas     = get_vidas(c["idempresa"], c["data_inicio"])
        aderencia = c["get_aderencia"]()
        print(f"  Vidas: {vidas} | Aderência: {aderencia}%")

        atualizado = False
        if vidas and vidas > 0:
            conteudo = atualizar_campo(conteudo, c["id"], "vidas", vidas)
            atualizado = True
        if aderencia and aderencia > 0:
            conteudo = atualizar_campo(conteudo, c["id"], "aderencia", aderencia)
            atualizado = True
        if atualizado and vidas and aderencia:
            conteudo = atualizar_historico(conteudo, c["id"], vidas, aderencia)

        # Sincronizar fornecedores novos
        novos_forn = 0
        if c.get("sd_table_id"):
            print(f"  Buscando fornecedores na tabela {c['sd_table_id']}...")
            fornecedores_mb = get_fornecedores_sd(c["sd_table_id"], idempresa=c["idempresa"])
            conteudo, novos_forn = sincronizar_fornecedores(conteudo, c["id"], fornecedores_mb)
            if novos_forn > 0:
                print(f"  +{novos_forn} fornecedor(es) novo(s) adicionado(s)")
                atualizado = True
                novos_por_cliente.append((c["nome"], novos_forn))

        relatorio_partes.append(formatar_cliente(
            nome=f"🔵 {c['nome']}", prazo=c["prazo"],
            vidas=vidas, vidas_meta=c["metaVidasF1"],
            aderencia=aderencia, aderencia_meta=c["metaAderencia"],
            atualizado=atualizado, novos_forn=novos_forn,
        ))

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
