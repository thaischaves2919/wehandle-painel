"""
Script de atualização automática do painel wehandle.
Roda via GitHub Actions diariamente às 07:45 BRT (10:45 UTC).
Queries Metabase API → atualiza dados.js → commit + push.
"""
import os
import re
import json
import datetime
import requests

MB_URL     = "https://mbwh.wehandle.com.br"
MB_SESSION = os.environ["MB_SESSION"]
TODAY    = datetime.date.today().isoformat()          # YYYY-MM-DD
TODAY_BR = datetime.date.today().strftime("%d/%m/%Y") # DD/MM/YYYY

session = requests.Session()
session.headers.update({
    "Content-Type": "application/json",
    "X-Metabase-Session": MB_SESSION
})

# ── 1. Verificar autenticação ──────────────────────────────────────────────────
def autenticar():
    r = session.get(f"{MB_URL}/api/user/current")
    if r.status_code == 200:
        nome = r.json().get("first_name", "usuário")
        print(f"✓ Autenticado no Metabase como {nome}")
    else:
        raise Exception(f"Token de sessão inválido ou expirado (status {r.status_code}). Renove o MB_SESSION no GitHub Secrets.")

# ── 2. Executar query Metabase (dataset endpoint) ──────────────────────────────
def run_query(database_id, native_sql):
    payload = {
        "database": database_id,
        "type": "native",
        "native": {"query": native_sql}
    }
    r = session.post(f"{MB_URL}/api/dataset", json=payload, timeout=60)
    if not r.ok:
        print(f"Erro {r.status_code} na query: {r.text[:500]}")
        r.raise_for_status()
    data = r.json()
    rows = data.get("data", {}).get("rows", [])
    cols = [c["name"] for c in data.get("data", {}).get("cols", [])]
    return [dict(zip(cols, row)) for row in rows]

DB_ID = 14  # Snowflake - DB wehandle

# ── 3. Vidas ───────────────────────────────────────────────────────────────────
def get_vidas(idempresa, data_inicio):
    sql = f"""
    SELECT COUNT(DISTINCT IDUSUARIO) AS vidas
    FROM SD_EMPRESA_USUARIO
    WHERE IDEMPRESA = {idempresa}
      AND PROJDESATIVADO <> 'S'
      AND USUDESATIVADO = FALSE
    """
    rows = run_query(DB_ID, sql)
    if rows:
        v = rows[0].get("VIDAS") or rows[0].get("vidas")
        if v is not None:
            return int(v)
    return None

# ── 4. Aderência ───────────────────────────────────────────────────────────────
ADERENTES_STATUS = ("'OK'", "'Em validação'", "'Isento'", "'Isenção'",
                    "'Perto Vencimento'", "'Vencido'", "'Aprovado'")

def get_aderencia(idempresa):
    status_in = ", ".join(ADERENTES_STATUS)
    sql = f"""
    SELECT
      SUM(CASE WHEN STATUS IN ({status_in}) THEN 1 ELSE 0 END) AS aderentes,
      COUNT(*) AS total
    FROM SD_DOCUMENTOS_ATIVO
    WHERE IDEMPRESA = {idempresa}
      AND STATUS <> 'Documento Substituido'
    """
    rows = run_query(DB_ID, sql)
    if rows:
        row = rows[0]
        aderentes = row.get("ADERENTES") or row.get("aderentes") or 0
        total     = row.get("TOTAL")     or row.get("total")     or 0
        if total and int(total) > 0:
            return round(int(aderentes) / int(total) * 100, 2)
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
    """Substitui o valor de 'campo' dentro do bloco do cliente_id."""
    # Localizar id: 'cliente_id'
    idx = conteudo.find(f"id: '{cliente_id}'")
    if idx == -1:
        return conteudo
    # Procurar o campo a partir da posição do cliente
    padrao = re.compile(rf"({re.escape(campo)}:\s*)([^,\n]+)")
    match = padrao.search(conteudo, idx)
    if match:
        novo = f"{match.group(1)}{valor}"
        conteudo = conteudo[:match.start()] + novo + conteudo[match.end():]
    return conteudo

def atualizar_data_comentario(conteudo, data_br):
    padrao = re.compile(r"// Última atualização:.*")
    return padrao.sub(f"// Última atualização: {data_br}", conteudo)

def atualizar_historico(conteudo, cliente_id, vidas, aderencia):
    """Adiciona ou atualiza entrada de hoje no historico do cliente."""
    # Localizar bloco historico do cliente
    idx_cliente = conteudo.find(f"id: '{cliente_id}'")
    if idx_cliente == -1:
        return conteudo

    idx_hist = conteudo.find("historico:", idx_cliente)
    if idx_hist == -1:
        return conteudo

    # Checar se já existe entrada com hoje
    entrada = f"{{ data: '{TODAY}', vidas: {vidas}, aderencia: {aderencia} }}"
    padrao_hoje = re.compile(rf"\{{\s*data:\s*'{re.escape(TODAY)}'[^}}]*\}}")

    # Encontrar o array historico — do [ até o ]
    inicio_arr = conteudo.find("[", idx_hist)
    fim_arr    = conteudo.find("]", inicio_arr)
    if inicio_arr == -1 or fim_arr == -1:
        return conteudo

    bloco = conteudo[inicio_arr:fim_arr + 1]

    if padrao_hoje.search(bloco):
        # Atualizar entrada existente
        bloco_novo = padrao_hoje.sub(entrada, bloco)
    else:
        # Inserir nova entrada antes do ]
        bloco_novo = bloco[:-1].rstrip() + ",\n        " + entrada + "\n      ]"

    return conteudo[:inicio_arr] + bloco_novo + conteudo[fim_arr + 1:]

# ── 6. Relatório / PushNotification ──────────────────────────────────────────
def formatar_cliente(nome, prazo, vidas, vidas_meta, aderencia, aderencia_meta, atualizado):
    if not atualizado:
        return f"\n{nome} (prazo: {prazo})\n  (sem atualização hoje)"
    falta_vidas = vidas_meta - vidas if vidas_meta else None
    gap_ader    = round(aderencia - aderencia_meta, 2) if aderencia_meta else None
    vidas_str   = f"Vidas: {vidas} | Meta F1: {vidas_meta} | " + \
                  ("✅" if falta_vidas is not None and falta_vidas <= 0
                   else f"Faltam: {falta_vidas}")
    ader_str    = f"Aderência: {aderencia}% | Meta: {aderencia_meta}% | " + \
                  ("✅" if gap_ader is not None and gap_ader >= 0
                   else f"Gap: {gap_ader} p.p.")
    return f"\n{nome} (prazo: {prazo})\n  {vidas_str}\n  {ader_str}"

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    autenticar()

    clientes = [
        {"id": "netzsch",     "nome": "Netzsch",     "idempresa": 34863,
         "metaVidasF1": 151,  "metaAderencia": 50,   "prazo": "06/06"},
        {"id": "saint-gobain","nome": "Saint-Gobain", "idempresa": 6,
         "metaVidasF1": 201,  "metaAderencia": 50,   "prazo": "06/06"},
        {"id": "gestamp",     "nome": "Gestamp",     "idempresa": 77911,
         "metaVidasF1": 201,  "metaAderencia": 50,   "prazo": "05/07"},
    ]

    conteudo = ler_dados()
    relatorio_partes = [f"📊 Relatório diário — {TODAY_BR}\n"]

    for c in clientes:
        print(f"\n--- {c['nome']} ---")
        vidas     = get_vidas(c["idempresa"], None)
        aderencia = get_aderencia(c["idempresa"])
        print(f"  Vidas: {vidas} | Aderência: {aderencia}%")

        atualizado = False
        if vidas is not None and vidas > 0:
            conteudo = atualizar_campo(conteudo, c["id"], "vidas", vidas)
            atualizado = True
        if aderencia is not None and aderencia > 0:
            conteudo = atualizar_campo(conteudo, c["id"], "aderencia", aderencia)
            atualizado = True
        if atualizado and vidas and aderencia:
            conteudo = atualizar_historico(conteudo, c["id"], vidas, aderencia)

        relatorio_partes.append(formatar_cliente(
            nome=f"🔵 {c['nome']}", prazo=c["prazo"],
            vidas=vidas, vidas_meta=c["metaVidasF1"],
            aderencia=aderencia, aderencia_meta=c["metaAderencia"],
            atualizado=atualizado
        ))

    # Atualizar comentário de data
    conteudo = atualizar_data_comentario(conteudo, TODAY_BR)

    salvar_dados(conteudo)
    print(f"\n✓ dados.js atualizado — {TODAY_BR}")

    relatorio = "\n".join(relatorio_partes)
    print("\n" + relatorio)

    # Guardar relatório para o workflow usar
    with open("relatorio.txt", "w", encoding="utf-8") as f:
        f.write(relatorio)

if __name__ == "__main__":
    main()
