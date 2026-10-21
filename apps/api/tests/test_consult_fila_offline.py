"""Fila offline (#12): reenviar o mesmo client_id não cria fala nova; a ordem do reenvio é preservada."""

from tests.conftest import login
from tests.test_consult import start


def _post(client, h, cid, items):
    res = client.post(f"/consultations/{cid}/segments", json={"segmentos": items}, headers=h)
    assert res.status_code == 200, res.text
    return res.json()


def test_reenviar_mesmo_client_id_nao_cria_fala(client):
    h = login(client)
    cid = start(client, h)["id"]
    fala = {"falante": "paciente", "texto": "Estou com dor no peito desde ontem.", "client_id": "c1-0001"}
    assert len(_post(client, h, cid, [fala])["novos"]) == 1
    again = _post(client, h, cid, [fala])
    assert again["novos"] == [] and again["total"] == 1
    got = client.get(f"/consultations/{cid}", headers=h).json()
    assert [s["n"] for s in got["segmentos"]] == [1]
    assert got["segmentos"][0]["client_id"] == "c1-0001"


def test_lote_reenviado_parcialmente_mantem_ordem_e_sem_duplicar(client):
    h = login(client)
    cid = start(client, h)["id"]
    falas = [{"falante": "medico", "texto": f"Fala número {i}.", "client_id": f"c1-{i:04d}"} for i in range(1, 4)]
    _post(client, h, cid, falas[:1])
    _post(client, h, cid, falas)  # a rede voltou: a fila reenvia tudo em ordem
    _post(client, h, cid, falas)
    segs = client.get(f"/consultations/{cid}", headers=h).json()["segmentos"]
    assert [s["texto"] for s in segs] == [f["texto"] for f in falas]
    assert [s["n"] for s in segs] == [1, 2, 3]


def test_client_id_repetido_no_mesmo_lote_e_invalido_e_opcional(client):
    h = login(client)
    cid = start(client, h)["id"]
    dup = {"falante": "medico", "texto": "Boa tarde.", "client_id": "x1"}
    assert len(_post(client, h, cid, [dup, dup])["novos"]) == 1
    assert len(_post(client, h, cid, [{"falante": "medico", "texto": "Sem id."}])["novos"]) == 1
    bad = client.post(f"/consultations/{cid}/segments", json={"segmentos": [{**dup, "client_id": "a b/<>"}]}, headers=h)
    assert bad.status_code == 422


def test_client_id_so_vale_dentro_da_propria_consulta(client):
    """O mesmo client_id em outra consulta não é descartado, e a lista da consulta A não vaza para a B."""
    h = login(client)
    a = start(client, h)["id"]
    fala = {"falante": "paciente", "texto": "Tenho alergia a dipirona.", "client_id": "c-igual"}
    assert len(_post(client, h, a, [fala])["novos"]) == 1
    client.post(f"/consultations/{a}/finish", json={"segmentos": []}, headers=h)
    b = start(client, h)["id"]
    assert b != a
    assert len(_post(client, h, b, [fala])["novos"]) == 1


def test_client_id_reaproveitado_com_outro_texto_nao_altera_a_fala_original(client):
    h = login(client)
    cid = start(client, h)["id"]
    _post(client, h, cid, [{"falante": "medico", "texto": "Original.", "client_id": "k-1"}])
    again = _post(client, h, cid, [{"falante": "medico", "texto": "Adulterada.", "client_id": "k-1"}])
    assert again["novos"] == []
    segs = client.get(f"/consultations/{cid}", headers=h).json()["segmentos"]
    assert [s["texto"] for s in segs] == ["Original."]


def test_finish_nao_duplica_fala_ja_enviada_pela_fila(client):
    h = login(client)
    cid = start(client, h)["id"]
    fala = {"falante": "paciente", "texto": "Sinto falta de ar.", "client_id": "k-fin"}
    _post(client, h, cid, [fala])
    res = client.post(f"/consultations/{cid}/finish", json={"segmentos": [fala]}, headers=h)
    assert res.status_code == 200, res.text
    assert [s["texto"] for s in res.json()["segmentos"]] == ["Sinto falta de ar."]
