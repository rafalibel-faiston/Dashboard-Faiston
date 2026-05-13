import psycopg2
import os
import hashlib
import random
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

def hash_senha(senha):
    return hashlib.sha256(senha.encode()).hexdigest()

conn = psycopg2.connect(os.environ.get("DATABASE_URL"))
cur = conn.cursor()

print("🌱 Iniciando seed...")

# Limpa dados antigos (mantém admin)
cur.execute("DELETE FROM tarefas")
cur.execute("DELETE FROM usuarios WHERE usuario != 'admin'")
conn.commit()

# Funcionários
funcionarios = [
    ("rafael", "rafael123", "Rafael Ribeiro Libel", "funcionario"),
    ("pedro",  "pedro123",  "Pedro Alves",          "funcionario"),
    ("ezequiel","ezequiel123","Ezequiel Santos",     "funcionario"),
    ("ronald", "ronald123", "Ronald Ferreira",       "funcionario"),
    ("ana",    "ana123",    "Ana Carolina",          "funcionario"),
    ("lucas",  "lucas123",  "Lucas Martins",         "gestor"),
]

ids = {}
for usuario, senha, nome, perfil in funcionarios:
    cur.execute(
        "INSERT INTO usuarios (usuario, senha_hash, nome, perfil) VALUES (%s,%s,%s,%s) RETURNING id",
        (usuario, hash_senha(senha), nome, perfil)
    )
    ids[nome] = cur.fetchone()[0]

conn.commit()
print(f"✅ {len(funcionarios)} usuários criados")

# Tarefas
clientes  = ["NTT", "Arcos Dourados", "Zamp", "Telcoweb"]
prioridades = ["Alta", "Alta", "Media", "Media", "Media", "Baixa"]
status_opts  = ["concluido", "concluido", "concluido", "em_andamento", "aberto"]

descricoes = [
    "Abertura de chamado no NOC",
    "Acompanhamento de incidente de rede",
    "Configuração de switch camada 2",
    "Monitoramento de links MPLS",
    "Troca de equipamento em campo",
    "Atualização de firmware",
    "Relatório semanal de disponibilidade",
    "Escalada para fornecedor",
    "Revisão de topologia de rede",
    "Acionamento de parceiro técnico",
    "Documentação de circuito",
    "Teste de failover",
    "Análise de log de roteador",
    "Criação de tickets no sistema",
    "Validação de SLA com cliente",
    "Suporte remoto a filial",
    "Instalação de CPE",
    "Diagnóstico de latência elevada",
    "Agendamento de manutenção",
    "Follow-up de chamado crítico",
]

tarefas_criadas = 0
now = datetime.now()

for func_nome, func_id in ids.items():
    if func_nome == "Lucas Martins":
        continue  # gestor não cria tarefas
    
    n_tarefas = random.randint(8, 15)
    for _ in range(n_tarefas):
        dias_atras = random.randint(0, 6)
        criado_em = now - timedelta(days=dias_atras, hours=random.randint(0,8), minutes=random.randint(0,59))
        cliente = random.choice(clientes)
        prioridade = random.choice(prioridades)
        status = random.choice(status_opts)
        descricao = random.choice(descricoes)
        
        # Horas apontadas (concluídas têm mais horas)
        if status == "concluido":
            segundos = random.randint(1800, 14400)  # 30min a 4h
        elif status == "em_andamento":
            segundos = random.randint(600, 5400)    # 10min a 1h30
        else:
            segundos = 0

        cur.execute("""
            INSERT INTO tarefas (usuario_id, descricao, cliente, prioridade, status, segundos, criado_em, atualizado_em)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """, (func_id, descricao, cliente, prioridade, status, segundos, criado_em, criado_em))
        tarefas_criadas += 1

conn.commit()
cur.close()
conn.close()
print(f"✅ {tarefas_criadas} tarefas criadas")
print("\n📋 Credenciais dos usuários:")
for usuario, senha, nome, perfil in funcionarios:
    print(f"  {perfil:12} | {usuario:12} / {senha}")
print(f"  {'admin':12} | {'admin':12} / admin123")