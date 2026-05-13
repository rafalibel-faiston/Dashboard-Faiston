from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import psycopg2
import os
from dotenv import load_dotenv

# 1. Carrega a senha do banco do arquivo .env
load_dotenv()

# 2. Inicializa o Motor do Sistema
app = FastAPI(title="Faiston Ops - API", version="1.0")

# --- CONEXÃO COM O BANCO DE DADOS ---
def get_db_connection():
    try:
        # Puxa a DATABASE_URL que está no .env (ou nas Variáveis do Railway)
        conn = psycopg2.connect(os.getenv("DATABASE_URL"))
        return conn
    except Exception as e:
        print(f"Erro Crítico de Banco: {e}")
        return None

# --- MODELOS DE DADOS (O formato que o JavaScript vai mandar) ---
class AcaoBackoffice(BaseModel):
    comando: str
    cliente: str = "Geral" # Futuramente vai puxar a aba ativa (NTT, Zamp, etc)

# --- ROTAS DA API (Os "Ouvidos" do Sistema) ---

@app.post("/api/registrar-acao")
def registrar_acao(acao: AcaoBackoffice):
    """
    Recebe o texto do 'Comando rápido' da tela e salva no PostgreSQL.
    """
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=500, detail="Banco de dados offline")
    
    try:
        cursor = conn.cursor()
        # Inserindo na tabela registros_atividades que você já tem criada
        cursor.execute(
            "INSERT INTO registros_atividades (acao, descricao, status, gravidade) VALUES (%s, %s, %s, %s)",
            ("Ação de Backoffice", acao.comando, "Aberto", "Normal")
        )
        conn.commit()
        cursor.close()
        conn.close()
        
        return {"sucesso": True, "mensagem": "Ação registrada com sucesso na base!"}
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/kpis")
def get_kpis():
    """
    Entrega os números atualizados para os cartões da tela web.
    """
    return {
        "clientes_ativos": 4,
        "tickets_abertos": 64,
        "time_alocado": 100,
        "sla_cumprido": 94
    }

# --- SERVINDO A TELA (Front-end) ---
# Dizemos ao Python onde encontrar a pasta com o seu design
app.mount("/css", StaticFiles(directory="static/css"), name="css")
app.mount("/js", StaticFiles(directory="static/js"), name="js")

# Quando acessar o link principal (localhost:8000), ele abre a sua tela
@app.get("/")
def serve_frontend():
    # Verifica se o arquivo HTML realmente está na pasta certa
    if not os.path.exists("static/index.html"):
        return {"erro": "Arquivo index.html não encontrado na pasta static!"}
    
    return FileResponse("static/index.html")