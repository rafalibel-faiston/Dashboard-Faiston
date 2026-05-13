from fastapi import FastAPI, HTTPException, Response, Cookie
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
import psycopg2
import os, hashlib, secrets
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()

app = FastAPI(title="Faiston Ops - API", version="1.0")

Path("static/css").mkdir(parents=True, exist_ok=True)
Path("static/js").mkdir(parents=True, exist_ok=True)

# Sessões em memória: token -> {usuario_id, nome, perfil}
sessions = {}

def get_db():
    try:
        return psycopg2.connect(os.environ.get("DATABASE_URL"))
    except Exception as e:
        print(f"Erro BD: {e}")
        return None

def hash_senha(senha):
    return hashlib.sha256(senha.encode()).hexdigest()

def get_session(token: str):
    return sessions.get(token)

# --- SETUP INICIAL DO BANCO ---
def setup_banco():
    conn = get_db()
    if not conn: return
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id SERIAL PRIMARY KEY,
                usuario VARCHAR(50) UNIQUE NOT NULL,
                senha_hash VARCHAR(64) NOT NULL,
                nome VARCHAR(100) NOT NULL,
                perfil VARCHAR(20) NOT NULL DEFAULT 'funcionario',
                ativo BOOLEAN DEFAULT TRUE,
                criado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tarefas (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER REFERENCES usuarios(id),
                descricao TEXT NOT NULL,
                cliente VARCHAR(50),
                prioridade VARCHAR(20) DEFAULT 'Media',
                status VARCHAR(30) DEFAULT 'aberto',
                segundos INTEGER DEFAULT 0,
                criado_em TIMESTAMP DEFAULT NOW(),
                atualizado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        # Cria admin padrão se não existir
        cur.execute("SELECT id FROM usuarios WHERE usuario = 'admin'")
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO usuarios (usuario, senha_hash, nome, perfil) VALUES (%s, %s, %s, %s)",
                ('admin', hash_senha('admin123'), 'Administrador', 'admin')
            )
            print("✅ Admin padrão criado: admin / admin123")
        conn.commit()
        cur.close()
        conn.close()
        print("✅ Banco configurado")
    except Exception as e:
        print(f"Erro setup: {e}")

setup_banco()

# --- MODELOS ---
class LoginRequest(BaseModel):
    usuario: str
    senha: str

class NovoUsuario(BaseModel):
    usuario: str
    senha: str
    nome: str
    perfil: str  # admin | gestor | funcionario

class AtualizarUsuario(BaseModel):
    nome: str
    perfil: str
    senha: str = ""
    ativo: bool = True

class TarefaModel(BaseModel):
    descricao: str
    cliente: str
    prioridade: str = "Media"
    status: str = "aberto"
    segundos: int = 0

class AtualizarSegundos(BaseModel):
    segundos: int

# --- AUTH ---
@app.post("/api/login")
def login(req: LoginRequest, response: Response):
    conn = get_db()
    if not conn:
        raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, nome, perfil FROM usuarios WHERE usuario=%s AND senha_hash=%s AND ativo=TRUE",
            (req.usuario, hash_senha(req.senha))
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            raise HTTPException(status_code=401, detail="Usuário ou senha inválidos")
        token = secrets.token_hex(32)
        sessions[token] = {"id": row[0], "nome": row[1], "perfil": row[2]}
        response.set_cookie("faiston_token", token, httponly=True, samesite="lax", max_age=86400)
        return {"sucesso": True, "perfil": row[2], "nome": row[1]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/logout")
def logout(response: Response, faiston_token: str = Cookie(None)):
    if faiston_token and faiston_token in sessions:
        del sessions[faiston_token]
    response.delete_cookie("faiston_token")
    return {"sucesso": True}

@app.get("/api/me")
def me(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess:
        raise HTTPException(status_code=401, detail="Não autenticado")
    return sess

# --- USUÁRIOS (só admin) ---
@app.get("/api/usuarios")
def listar_usuarios(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] != "admin":
        raise HTTPException(status_code=403, detail="Acesso negado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, usuario, nome, perfil, ativo, criado_em FROM usuarios ORDER BY criado_em DESC")
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [{"id": r[0], "usuario": r[1], "nome": r[2], "perfil": r[3], "ativo": r[4], "criado_em": str(r[5])} for r in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/usuarios")
def criar_usuario(u: NovoUsuario, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] != "admin":
        raise HTTPException(status_code=403, detail="Acesso negado")
    if u.perfil not in ("admin", "gestor", "funcionario"):
        raise HTTPException(status_code=400, detail="Perfil inválido")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO usuarios (usuario, senha_hash, nome, perfil) VALUES (%s, %s, %s, %s) RETURNING id",
            (u.usuario, hash_senha(u.senha), u.nome, u.perfil)
        )
        new_id = cur.fetchone()[0]
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True, "id": new_id}
    except psycopg2.errors.UniqueViolation:
        raise HTTPException(status_code=400, detail="Usuário já existe")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/usuarios/{uid}")
def atualizar_usuario(uid: int, u: AtualizarUsuario, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] != "admin":
        raise HTTPException(status_code=403, detail="Acesso negado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        if u.senha:
            cur.execute("UPDATE usuarios SET nome=%s, perfil=%s, ativo=%s, senha_hash=%s WHERE id=%s",
                        (u.nome, u.perfil, u.ativo, hash_senha(u.senha), uid))
        else:
            cur.execute("UPDATE usuarios SET nome=%s, perfil=%s, ativo=%s WHERE id=%s",
                        (u.nome, u.perfil, u.ativo, uid))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/usuarios/{uid}")
def deletar_usuario(uid: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess or sess["perfil"] != "admin":
        raise HTTPException(status_code=403, detail="Acesso negado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM usuarios WHERE id=%s", (uid,))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- TAREFAS ---
@app.get("/api/tarefas")
def listar_tarefas(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        if sess["perfil"] in ("admin", "gestor"):
            cur.execute("""
                SELECT t.id, t.descricao, t.cliente, t.prioridade, t.status, t.segundos, t.criado_em, u.nome
                FROM tarefas t JOIN usuarios u ON t.usuario_id = u.id
                ORDER BY t.criado_em DESC
            """)
        else:
            cur.execute("""
                SELECT t.id, t.descricao, t.cliente, t.prioridade, t.status, t.segundos, t.criado_em, u.nome
                FROM tarefas t JOIN usuarios u ON t.usuario_id = u.id
                WHERE t.usuario_id = %s ORDER BY t.criado_em DESC
            """, (sess["id"],))
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [{"id": r[0], "descricao": r[1], "cliente": r[2], "prioridade": r[3],
                 "status": r[4], "segundos": r[5], "criado_em": str(r[6]), "funcionario": r[7]} for r in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/tarefas")
def criar_tarefa(t: TarefaModel, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO tarefas (usuario_id, descricao, cliente, prioridade, status, segundos) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            (sess["id"], t.descricao, t.cliente, t.prioridade, t.status, t.segundos)
        )
        new_id = cur.fetchone()[0]
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True, "id": new_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/tarefas/{tid}")
def atualizar_tarefa(tid: int, t: TarefaModel, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE tarefas SET descricao=%s, cliente=%s, prioridade=%s, status=%s, segundos=%s, atualizado_em=NOW() WHERE id=%s AND usuario_id=%s",
            (t.descricao, t.cliente, t.prioridade, t.status, t.segundos, tid, sess["id"])
        )
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.patch("/api/tarefas/{tid}/segundos")
def atualizar_segundos(tid: int, body: AtualizarSegundos, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        cur.execute("UPDATE tarefas SET segundos=%s, atualizado_em=NOW() WHERE id=%s AND usuario_id=%s",
                    (body.segundos, tid, sess["id"]))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/tarefas/{tid}")
def deletar_tarefa(tid: int, faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    conn = get_db()
    if not conn: raise HTTPException(status_code=500, detail="Banco offline")
    try:
        cur = conn.cursor()
        owner_check = "" if sess["perfil"] in ("admin", "gestor") else "AND usuario_id=%s"
        if sess["perfil"] in ("admin", "gestor"):
            cur.execute("DELETE FROM tarefas WHERE id=%s", (tid,))
        else:
            cur.execute("DELETE FROM tarefas WHERE id=%s AND usuario_id=%s", (tid, sess["id"]))
        conn.commit(); cur.close(); conn.close()
        return {"sucesso": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- KPIs ---
@app.get("/api/kpis")
def get_kpis(faiston_token: str = Cookie(None)):
    sess = get_session(faiston_token)
    if not sess: raise HTTPException(status_code=401, detail="Não autenticado")
    return {"clientes_ativos": 4, "tickets_abertos": 64, "time_alocado": 100, "sla_cumprido": 94}

@app.get("/api/health")
def health():
    return {"status": "ok"}

# --- PÁGINAS ---
@app.get("/")
def root():
    return FileResponse("static/login.html")

@app.get("/dashboard")
def dashboard():
    return FileResponse("static/index.html")

@app.get("/funcionario")
def funcionario():
    return FileResponse("static/funcionario.html")

@app.get("/admin")
def admin_page():
    return FileResponse("static/admin.html")

app.mount("/css", StaticFiles(directory="static/css"), name="css")
app.mount("/js", StaticFiles(directory="static/js"), name="js")