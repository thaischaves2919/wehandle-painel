"""
Atualiza vidas por fornecedor em dados.js usando GOLD_VIDAS_ALOCADAS_DIARIO.
Fonte oficial: mesma tabela usada pelo card "Movimentação de Fornecedores Total Período".
Pode ser rodado manualmente ou chamado pelo update_dados.py.
"""
import os
import re
import json
import requests

MB_URL     = "https://mbwh.wehandle.com.br"
MB_SESSION = os.environ.get("MB_SESSION", "")

CLIENTES = [
    {"id": "netzsch",     "idempresa": "34863"},
    {"id": "saint-gobain","idempresa": "6"},
    {"id": "gestamp",     "idempresa": "77911"},
    {"id": "grupo-zelo",  "idempresa": "78024"},
    {"id": "melitta",     "idempresa": "79099"},
]

DADOS_JS = os.path.join(os.path.dirname(__file__), "..", "dados.js")


def run_sql(sql):
    """Executa SQL nativo no Snowflake via Metabase (database 14)."""
    headers = {"Content-Type": "application/json", "X-Metabase-Session": MB_SESSION}
    body = {"database": 14, "type": "native", "native": {"query": sql}}
    r = requests.post(f"{MB_URL}/api/dataset", headers=headers, json=body, timeout=60)
    if not r.ok:
        raise Exception(f"Erro {r.status_code}: {r.text[:200]}")
    data = r.json().get("data", {})
    cols = [c["name"].upper() for c in data.get("cols", [])]
    rows = data.get("rows", [])
    return [dict(zip(cols, row)) for row in rows]


def get_vidas_diario(idempresa):
    """Retorna {nome_upper: vidas} do dia mais recente em GOLD_VIDAS_ALOCADAS_DIARIO."""
    sql = f"""
    SELECT UPPER(TRIM(NM_EMPRESA_FORNECEDOR)) AS NOME, SUM(QTD_VIDAS) AS VIDAS
    FROM PROD_ANALYTICS.GOLD.GOLD_VIDAS_ALOCADAS_DIARIO
    WHERE REF_ID_EMPRESA_TOMADOR_PRINCIPAL = '{idempresa}'
      AND DT_DIA_REFERENCIA = (
          SELECT MAX(DT_DIA_REFERENCIA)
          FROM PROD_ANALYTICS.GOLD.GOLD_VIDAS_ALOCADAS_DIARIO
          WHERE REF_ID_EMPRESA_TOMADOR_PRINCIPAL = '{idempresa}'
      )
    GROUP BY UPPER(TRIM(NM_EMPRESA_FORNECEDOR))
    HAVING SUM(QTD_VIDAS) > 0
    """
    rows = run_sql(sql)
    return {r["NOME"]: int(r["VIDAS"] or 0) for r in rows if r.get("NOME")}


def match_nome(razao_social, vidas_map):
    """Casa nome do fornecedor com chave do vidas_map (substring case-insensitive)."""
    nome = razao_social.upper().strip()
    if nome in vidas_map:
        return vidas_map[nome]
    for nome_gold, vidas in vidas_map.items():
        if nome_gold in nome or nome in nome_gold:
            return vidas
    return None


def atualizar_cliente(conteudo, cliente_id, vidas_map):
    """Atualiza campo vidas: de cada fornecedor do cliente no conteúdo de dados.js."""
    idx_cli = conteudo.find(f"id: '{cliente_id}'")
    if idx_cli == -1:
        return conteudo, 0
    idx_fb = conteudo.find("fornecedores:", idx_cli)
    if idx_fb == -1:
        return conteudo, 0
    inicio = conteudo.find("[", idx_fb)
    prox = re.search(r"\n    \{", conteudo[idx_fb + 200:])
    fim_busca = (idx_fb + 200 + prox.start()) if prox else len(conteudo)
    fim = conteudo.rfind("]", inicio, fim_busca)
    if inicio == -1 or fim == -1:
        return conteudo, 0

    bloco_orig = conteudo[inicio:fim + 1]
    bloco = bloco_orig
    atualizados = 0

    for m in re.finditer(r"\{[^{}]*cnpj:\s*'([^']+)'[^{}]*\}", bloco_orig):
        linha = m.group(0)
        m_nome = re.search(r"razaoSocial:\s*'([^']+)'", linha)
        if not m_nome:
            continue
        vidas_val = match_nome(m_nome.group(1), vidas_map)
        if vidas_val is not None:
            nova = re.sub(r'\bvidas:\s*\d+', f'vidas: {vidas_val}', linha)
            if nova != linha:
                bloco = bloco.replace(linha, nova, 1)
                atualizados += 1

    return conteudo.replace(bloco_orig, bloco, 1), atualizados


def main():
    with open(DADOS_JS, "r", encoding="utf-8") as f:
        conteudo = f.read()

    total = 0
    for cli in CLIENTES:
        print(f"  Buscando vidas para {cli['id']} ({cli['idempresa']})...")
        try:
            vidas_map = get_vidas_diario(cli["idempresa"])
            conteudo, n = atualizar_cliente(conteudo, cli["id"], vidas_map)
            print(f"    → {n} fornecedores atualizados (total no dia: {sum(vidas_map.values())}v)")
            total += n
        except Exception as e:
            print(f"    ✗ Erro: {e}")

    with open(DADOS_JS, "w", encoding="utf-8") as f:
        f.write(conteudo)

    print(f"\nConcluído: {total} fornecedores atualizados em dados.js")


if __name__ == "__main__":
    main()
