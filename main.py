from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import psycopg2
import os
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Faiston Ops - API", version="1.0")

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
    return {"status": "ok"}

@app.get("/")
def serve_frontend():
    return FileResponse("static/index.html")

app.mount("/css", StaticFiles(directory="static/css"), name="css")
app.mount("/js", StaticFiles(directory="static/js"), name="js")
app.mount("/static", StaticFiles(directory="static"), name="static")

if __name__ == "__main__":
    import uvicorn
    # Railway injeta PORT automaticamente — nunca use valor fixo
    port = int(os.environ["PORT"])
    print(f"Iniciando na porta {port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")