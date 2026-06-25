import re

filepath = r'C:\Users\ThaisChavesCosta\OneDrive - wehandle\Área de Trabalho\wehandle-painel\dados.js'

with open(filepath, 'r', encoding='utf-8') as f:
    content = f.read()

# CNPJ → vidas (fonte: GOLD_VIDAS, consulta 2026-06-25)
updates = {
    # Netzsch
    '01.657.361/0001-78': 21,  # BRASIL SUL
    '76.315.688/0001-60': 6,   # SERRALHERIA VOLKHOFF
    '02.295.031/0001-42': 7,   # RC PINTURAS
    '38.234.347/0001-71': 10,  # CRANES SERVICE
    '00.877.679/0001-00': 5,   # WPS CONSULTORIA
    '05.974.479/0001-80': 9,   # KMCC
    '72.114.903/0001-04': 2,   # EXTIMBRAS
    '20.792.878/0001-14': 12,  # ELETROBLU
    '85.143.105/0001-52': 19,  # ORTUS
    '07.293.323/0001-60': 3,   # REDETEC
    '40.667.224/0001-76': 4,   # INTEC
    '80.650.633/0001-84': 4,   # ENGETEL
    '27.438.509/0001-77': 2,   # GLOBAL SPINDLE
    '06.374.470/0001-00': 8,   # GDV CONSTRUÇÕES
    '77.998.912/0008-03': 1,   # MASTER VIGILÂNCIA
    '82.662.263/0007-16': 6,   # BERMO
    '04.594.010/0001-53': 1,   # DMG MORI
    '85.179.620/0001-92': 5,   # PROTAL
    '78.214.905/0001-51': 10,  # AR CONDICIONADO WPS
    '14.228.109/0001-95': 3,   # CTNR
    '01.964.690/0001-61': 2,   # TRANSPOTECH
    '05.298.486/0001-00': 3,   # ORGANICA
    '82.321.845/0001-43': 2,   # SIMILAR
    '10.942.926/0001-50': 1,   # ProUPS
    '12.373.982/0001-46': 10,  # GLOBAL SEGURANÇA
    '15.674.133/0001-10': 6,   # NR SAFETY
    '81.329.823/0001-67': 1,   # SKA AUTOMAÇÃO
    '60.586.450/0001-30': 4,   # B. GROB
    '80.737.695/0001-28': 5,   # MENDES E BARCELOS
    '07.497.580/0001-13': 2,   # ATITUDE
    '05.263.279/0001-10': 6,   # TERRAPLANAGEM KNOPF
    '42.260.964/0001-19': 11,  # HZ HAUSBAU
    '05.017.262/0001-82': 1,   # SUL SERVICE
    '02.216.876/0002-86': 1,   # ANDRITZ
    '05.254.863/0001-09': 1,   # ANTUNES E GONCALVES
    '07.006.512/0001-04': 1,   # KR TREINAMENTOS
    '07.172.796/0001-09': 1,   # OKTA7
    '35.724.587/0001-66': 2,   # MIG CONSULTORIA
    '76.839.356/0001-85': 6,   # BALANTEC
    '08.175.349/0001-76': 2,   # B.LOTTI
    '06.964.752/0001-59': 3,   # P3 ENGENHARIA
    # Saint-Gobain
    '17.862.290/0001-85': 19,  # AMBPAV
    # Gestamp
    '04.407.579/0001-62': 15,  # GH DO BRASIL
    '30.058.500/0003-07': 13,  # LUZA GROUP
    '34.913.121/0001-46': 11,  # ELETROTAU
    '00.715.152/0001-70': 8,   # RODRIGUES
    '11.565.394/0001-41': 6,   # TRANSFORMA CORPORATE
    '12.264.795/0001-24': 5,   # TREAL
    '51.423.938/0001-55': 3,   # RFM
    '00.612.363/0001-88': 2,   # DATA PLUS
    '65.511.078/0001-16': 1,   # JCT SERVICES
    '73.014.607/0001-02': 1,   # MACONTRIN
    '23.823.459/0001-90': 1,   # FLX TECNOLOGIA
    '02.973.703/0001-21': 1,   # SERVNEWS
    # Grupo Zelo
    '08.562.228/0001-87': 13,  # TRIUNFO
    '07.655.416/0001-97': 28,  # ARTEBRILHO
    # Melitta
    '41.383.971/0001-45': 10,  # QUANTUM
    '49.930.514/0001-35': 1,   # SODEXO (filial principal)
}

changes = 0
not_found = []

for cnpj, vidas in updates.items():
    cnpj_escaped = re.escape(cnpj)
    pattern = r"(cnpj: '" + cnpj_escaped + r"'.*?vidas: )\d+"
    replacement = r'\g<1>' + str(vidas)
    new_content = re.sub(pattern, replacement, content, flags=re.DOTALL)
    if new_content == content:
        not_found.append(cnpj)
    else:
        content = new_content
        changes += 1

# Atualiza versão
content = content.replace("versao: '20260625d'", "versao: '20260625e'")

with open(filepath, 'w', encoding='utf-8') as f:
    f.write(content)

print(f"Atualizados: {changes} fornecedores")
if not_found:
    print(f"Não encontrados: {not_found}")
print("Concluído!")
