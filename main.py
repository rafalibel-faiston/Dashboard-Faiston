from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
import psycopg2
import os
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()

app = FastAPI(title="Faiston Ops - API", version="1.0")

# Garante que as pastas existem ao iniciar
Path("static/css").mkdir(parents=True, exist_ok=True)
Path("static/js").mkdir(parents=True, exist_ok=True)

def get_db_connection():
    try:
        conn = psycopg2.connect(os.environ.get("DATABASE_URL"))
        return conn
    except Exception as e:
        print(f"Erro Crítico de Banco: {e}")
        return None

class AcaoBackoffice(BaseModel):
    comando: str
    cliente: str = "Geral"

@app.post("/api/registrar-acao")
def registrar_acao(acao: AcaoBackoffice):
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=500, detail="Banco de dados offline")
    try:
        cursor = conn.cursor()
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
    return {
        "clientes_ativos": 4,
        "tickets_abertos": 64,
        "time_alocado": 100,
        "sla_cumprido": 94
    }

@app.get("/api/health")
def health_check():
    # Diagnóstico: mostra o que existe no filesystem
    files = []
    for root, dirs, filenames in os.walk("."):
        for f in filenames:
            files.append(os.path.join(root, f))
    return {"status": "ok", "files": files, "cwd": os.getcwd()}

@app.get("/")
def serve_frontend():
    index_path = "static/index.html"
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return HTMLResponse("<h1>static/index.html não encontrado</h1><p>Verifique se os arquivos foram commitados no repositório.</p>", status_code=404)

# Monta estáticos só se as pastas existirem
if os.path.exists("static/css"):
    app.mount("/css", StaticFiles(directory="static/css"), name="css")
if os.path.exists("static/js"):
    app.mount("/js", StaticFiles(directory="static/js"), name="js")
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")