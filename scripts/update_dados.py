"""
Script de atualização automática do painel wehandle.
Roda via GitHub Actions 3x por dia (08h, 13h e 18h BRT).
Usa Metabase Card API com token de sessão SSO → atualiza dados.js → commit + push.
Só atualiza clientes cujo período de 45 dias ainda não encerrou.
"""
import os
import re
import datetime
import requests

MB_URL     = "https://mbwh.wehandle.com.br"
MB_SESSION = os.environ["MB_SESSION"]
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
CLIENTES_VIDAS = {
    "netzsch":     {"idempresa": 34863, "data_inicio": "2026-04-22"},
    "saint-gobain":{"idempresa": 6,     "data_inicio": "2026-04-22"},
    "gestamp":     {"idempresa": 77911, "data_inicio": "2026-05-21"},
}

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
    """Card 30553 Histórico — última linha = mês atual."""
    rows = run_card(30553)
    if rows:
        ultimo = rows[-1]
        perc = ultimo.get("PORCENTAGEM_ADERENTE")
        if perc is not None:
            # valor como 0.617021 → 61.7
            return round(float(str(perc).replace(",", ".")) * 100, 2)
    return None

def get_aderencia_por_cards(card_ader, card_nao_ader):
    """Calcula aderência a partir de dois cards de contagem."""
    r_ad = run_card(card_ader)
    r_na = run_card(card_nao_ader)
    if r_ad and r_na:
        ad = int(r_ad[0].get("documentos") or r_ad[0].get(list(r_ad[0].keys())[0]) or 0)
        na = int(r_na[0].get("documentos") or r_na[0].get(list(r_na[0].keys())[0]) or 0)
        total = ad + na
        if total > 0:
            return round(ad / total * 100, 2)
    return None

# ── 5. Ler e atualizar dados.js ────────────────────────────────────────────────
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

# ── 6. Relatório ───────────────────────────────────────────────────────────────
def formatar_cliente(nome, prazo, vidas, vidas_meta, aderencia, aderencia_meta, atualizado):
    if not atualizado:
        return f"\n{nome} (prazo: {prazo})\n  (sem atualização hoje)"
    falta_vidas = (vidas_meta - vidas) if vidas_meta else None
    gap_ader    = round(aderencia - aderencia_meta, 2) if aderencia_meta else None
    vidas_str = f"Vidas: {vidas} | Meta F1: {vidas_meta} | " + (
        "✅" if falta_vidas is not None and falta_vidas <= 0 else f"Faltam: {falta_vidas}"
    )
    ader_str = f"Aderência: {aderencia}% | Meta: {aderencia_meta}% | " + (
        "✅" if gap_ader is not None and gap_ader >= 0 else f"Gap: {gap_ader} p.p."
    )
    return f"\n{nome} (prazo: {prazo})\n  {vidas_str}\n  {ader_str}"

# ── 6. Verificar se cliente está dentro do período de 45 dias ─────────────────
def dentro_do_prazo(data_inicio_str):
    data_inicio = datetime.date.fromisoformat(data_inicio_str)
    prazo = data_inicio + datetime.timedelta(days=45)
    hoje = datetime.date.today()
    return hoje <= prazo

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    autenticar()

    clientes = [
        {
            "id": "netzsch", "nome": "Netzsch", "prazo": "06/06",
            "idempresa": 34863, "data_inicio": "2026-04-22",
            "metaVidasF1": 151, "metaAderencia": 50,
            "get_aderencia": lambda: get_aderencia_netzsch(),
        },
        {
            "id": "saint-gobain", "nome": "Saint-Gobain", "prazo": "06/06",
            "idempresa": 6, "data_inicio": "2026-04-22",
            "metaVidasF1": 201, "metaAderencia": 50,
            "get_aderencia": lambda: get_aderencia_por_cards(31360, 31363),
        },
        {
            "id": "gestamp", "nome": "Gestamp", "prazo": "05/07",
            "idempresa": 77911, "data_inicio": "2026-05-21",
            "metaVidasF1": 201, "metaAderencia": 50,
            "get_aderencia": lambda: get_aderencia_por_cards(38145, 38154),
        },
    ]

    conteudo = ler_dados()
    relatorio_partes = [f"📊 Relatório diário — {TODAY_BR}\n"]

    for c in clientes:
        # Pular clientes fora do período de 45 dias
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

        relatorio_partes.append(formatar_cliente(
            nome=f"🔵 {c['nome']}", prazo=c["prazo"],
            vidas=vidas, vidas_meta=c["metaVidasF1"],
            aderencia=aderencia, aderencia_meta=c["metaAderencia"],
            atualizado=atualizado,
        ))

    conteudo = atualizar_data_comentario(conteudo, TODAY_BR)
    salvar_dados(conteudo)
    print(f"\n✓ dados.js atualizado — {TODAY_BR}")

    relatorio = "\n".join(relatorio_partes)
    print("\n" + relatorio)
    with open("relatorio.txt", "w", encoding="utf-8") as f:
        f.write(relatorio)

if __name__ == "__main__":
    main()
