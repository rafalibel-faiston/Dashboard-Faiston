"""
Minha Equipe mostra só a área de quem está logado -- inclusive pra admin/dev,
que veem a empresa inteira apenas em Admin → Usuários.
"""
from tests.test_areas import area_factory, usuario_factory  # noqa: F401 (fixtures)


def _ids(client, **params):
    resp = client.get("/api/usuarios", params=params)
    assert resp.status_code == 200, resp.text
    return {u["id"] for u in resp.json()}


def test_gestor_ve_so_a_propria_area(area_factory, usuario_factory):
    a, b = area_factory(), area_factory()
    gestor = usuario_factory(a["nome"], perfil="gestor")
    colega = usuario_factory(a["nome"])
    de_fora = usuario_factory(b["nome"])
    ids = _ids(gestor["client"], escopo="minha_area")
    assert colega["id"] in ids and gestor["id"] in ids
    assert de_fora["id"] not in ids
    assert _ids(gestor["client"]) == ids  # sem o parâmetro, mesma regra


def test_admin_minha_equipe_so_a_area_dele(admin_client, area_factory, usuario_factory):
    outra = area_factory()
    de_fora = usuario_factory(outra["nome"])
    assert de_fora["id"] not in _ids(admin_client, escopo="minha_area")
    assert de_fora["id"] in _ids(admin_client)  # Admin → Usuários continua completo


def test_renomear_area_nao_deixa_gestor_com_lista_velha(admin_client, area_factory, usuario_factory):
    area = area_factory()
    gestor = usuario_factory(area["nome"], perfil="gestor")
    colega = usuario_factory(area["nome"])
    novo = area["nome"] + "-R"
    resp = admin_client.put(f"/api/areas/{area['id']}", json={"nome": novo, "usa_projetos": True, "ativo": True})
    assert resp.status_code == 200, resp.text
    assert colega["id"] in _ids(gestor["client"], escopo="minha_area")
