"""Page de réservation /reserver : l'utilisateur choisit, vérifie et paie lui-même."""

import asyncio
import json

import pytest

from .conftest import DEMAIN, DUFFEL, MOT_DE_PASSE, URL_PASSAGERS, URL_RESERVATIONS

CONTACTS = [
    {
        "id": "recJEAN",
        "fields": {
            "Nom": "Dupont",
            "Prénom": "Jean",
            "Titre": "Monsieur",
            "Genre": "Homme",
            "Date_naissance": "1985-03-12",
            "Email": "jean.dupont@exemple.com",
            "Téléphone": "+33 6 12 34 56 78",
        },
    },
    {
        "id": "recLEA",
        "fields": {
            "Nom": "Dupont",
            "Prénom": "Léa",
            "Titre": "miss",
            "Genre": "f",
            "Date_naissance": "2012-06-01",
            "Email": "jean.dupont@exemple.com",
            "Téléphone": "0033612345678",
        },
    },
    {
        "id": "recSANSTEL",
        "fields": {
            "Nom": "Martin",
            "Prénom": "Paul",
            "Titre": "mr",
            "Genre": "m",
            "Date_naissance": "1990-01-01",
            "Email": "paul@exemple.com",
        },
    },
]
CHOIX_OK = {"passager_pas_1": "recJEAN", "passager_pas_2": "recLEA"}


@pytest.fixture
def duffel(http, offre, commande):
    recherche = http.post(f"{DUFFEL}/air/offer_requests").respond(
        json={"data": {"offers": [offre]}}
    )
    http.get(f"{DUFFEL}/air/offers/off_123").respond(json={"data": offre})
    ordre = http.post(f"{DUFFEL}/air/orders").respond(json={"data": commande})
    http.get(URL_PASSAGERS).respond(json={"records": CONTACTS})
    http.post(URL_RESERVATIONS).respond(json={"records": [{"id": "rec1"}, {"id": "rec2"}]})
    return {"recherche": recherche, "ordre": ordre}


async def _connecter(navigateur):
    reponse = await navigateur.post("/reserver/connexion", data={"password": MOT_DE_PASSE})
    assert reponse.status_code == 303
    assert "httponly" in reponse.headers["set-cookie"].lower()
    assert "samesite=strict" in reponse.headers["set-cookie"].lower()


async def _rechercher_avec_assistant(client):
    resultat = await client.call_tool(
        "rechercher_vols",
        {
            "code_iata_depart": "CDG",
            "code_iata_arrivee": "JFK",
            "date_aller": DEMAIN.isoformat(),
            "passagers": [{"type": "adult"}, {"age": 14}],
        },
    )
    return resultat.data


async def test_connexion_requise(navigateur, duffel):
    page = await navigateur.get("/reserver")
    assert 'name="password"' in page.text
    assert "Offre n°" not in page.text

    refus = await navigateur.post("/reserver/connexion", data={"password": "faux"})
    assert refus.status_code == 401

    # Sans session, impossible de voir une offre ou de payer.
    offre = await navigateur.get("/reserver/offre/off_123")
    assert offre.status_code == 303
    payer = await navigateur.post(
        "/reserver/offre/off_123", data={"action": "payer", **CHOIX_OK}
    )
    assert payer.status_code == 303
    assert not duffel["ordre"].called


async def test_offres_de_l_assistant_sur_la_page(client, navigateur, duffel):
    await _connecter(navigateur)
    vide = await navigateur.get("/reserver")
    assert "Aucune offre en cours" in vide.text

    resultat = await _rechercher_avec_assistant(client)
    assert resultat["offres"][0]["numero"] == 1

    page = await navigateur.get("/reserver")
    assert "Offre n°1 — 245.30 EUR" in page.text
    assert "CDG → JFK" in page.text
    assert "/reserver/offre/off_123" in page.text


async def test_parcours_complet(client, navigateur, duffel):
    await _connecter(navigateur)
    await _rechercher_avec_assistant(client)

    selection = await navigateur.get("/reserver/offre/off_123")
    assert "Passager 1 — adulte" in selection.text
    assert "Passager 2 — enfant de 14 an(s)" in selection.text
    assert "Jean Dupont (né(e) le 1985-03-12)" in selection.text

    recap = await navigateur.post(
        "/reserver/offre/off_123", data={"action": "verifier", **CHOIX_OK}
    )
    assert recap.status_code == 200
    assert "Payer 245.30 EUR" in recap.text
    assert "Jean Dupont, Léa Dupont" in recap.text
    assert not duffel["ordre"].called

    confirmation = await navigateur.post(
        "/reserver/offre/off_123", data={"action": "payer", **CHOIX_OK}
    )
    assert confirmation.status_code == 200
    assert "Réservation confirmée et payée" in confirmation.text
    assert "ABC123" in confirmation.text

    envoye = json.loads(duffel["ordre"].calls.last.request.content)["data"]
    assert envoye["payments"] == [{"type": "balance", "currency": "EUR", "amount": "245.30"}]
    assert envoye["passengers"] == [
        {
            "id": "pas_1",
            "title": "mr",
            "gender": "m",
            "given_name": "Jean",
            "family_name": "Dupont",
            "born_on": "1985-03-12",
            "email": "jean.dupont@exemple.com",
            "phone_number": "+33612345678",
        },
        {
            "id": "pas_2",
            "title": "miss",
            "gender": "f",
            "given_name": "Léa",
            "family_name": "Dupont",
            "born_on": "2012-06-01",
            "email": "jean.dupont@exemple.com",
            "phone_number": "+33612345678",
        },
    ]

    # L'assistant retrouve ensuite la réservation enregistrée.
    rejoue = await navigateur.post(
        "/reserver/offre/off_123", data={"action": "payer", **CHOIX_OK}
    )
    assert "ABC123" in rejoue.text
    assert duffel["ordre"].call_count == 1


async def test_double_clic_un_seul_paiement(client, navigateur, duffel):
    await _connecter(navigateur)
    await _rechercher_avec_assistant(client)
    pages = await asyncio.gather(
        navigateur.post("/reserver/offre/off_123", data={"action": "payer", **CHOIX_OK}),
        navigateur.post("/reserver/offre/off_123", data={"action": "payer", **CHOIX_OK}),
    )
    assert duffel["ordre"].call_count == 1
    assert all("ABC123" in p.text for p in pages)


@pytest.mark.parametrize(
    ("choix", "message"),
    [
        ({"passager_pas_1": "recJEAN", "passager_pas_2": "recJEAN"}, "deux places"),
        ({"passager_pas_1": "recLEA", "passager_pas_2": "recJEAN"}, "place adulte"),
        ({"passager_pas_1": "recSANSTEL", "passager_pas_2": "recLEA"}, "format international"),
        ({"passager_pas_1": "recJEAN"}, "chaque place"),
    ],
)
async def test_selection_invalide(client, navigateur, duffel, choix, message):
    await _connecter(navigateur)
    await _rechercher_avec_assistant(client)
    page = await navigateur.post("/reserver/offre/off_123", data={"action": "payer", **choix})
    assert page.status_code == 400
    assert message in page.text
    assert not duffel["ordre"].called


async def test_prix_modifie_avant_paiement(client, navigateur, http, duffel, offre):
    await _connecter(navigateur)
    await _rechercher_avec_assistant(client)
    http.get(f"{DUFFEL}/air/offers/off_123").respond(
        json={"data": {**offre, "total_amount": "300.00"}}
    )
    page = await navigateur.post(
        "/reserver/offre/off_123", data={"action": "payer", **CHOIX_OK}
    )
    assert page.status_code == 409
    assert "300.00 EUR" in page.text
    assert not duffel["ordre"].called


async def test_offre_inconnue(navigateur, duffel):
    await _connecter(navigateur)
    page = await navigateur.get("/reserver/offre/off_inconnue")
    assert page.status_code == 404
    assert "Rien n&#x27;a été payé" in page.text


async def test_recherche_depuis_la_page(navigateur, duffel):
    await _connecter(navigateur)
    reponse = await navigateur.post(
        "/reserver/recherche",
        data={
            "depart": "cdg",
            "arrivee": "JFK",
            "date_aller": DEMAIN.isoformat(),
            "date_retour": "",
            "adultes": "1",
            "ages_enfants": "14",
            "classe": "business",
        },
    )
    assert reponse.status_code == 303
    envoye = json.loads(duffel["recherche"].calls.last.request.content)["data"]
    assert envoye["passengers"] == [{"type": "adult"}, {"age": 14}]
    assert envoye["cabin_class"] == "business"
    assert envoye["slices"][0]["origin"] == "CDG"
    page = await navigateur.get("/reserver")
    assert "Offre n°1" in page.text


async def test_recherche_invalide_depuis_la_page(navigateur, duffel):
    await _connecter(navigateur)
    page = await navigateur.post(
        "/reserver/recherche",
        data={"depart": "Paris", "arrivee": "JFK", "date_aller": DEMAIN.isoformat()},
    )
    assert page.status_code == 400
    assert "codes IATA" in page.text
    assert not duffel["recherche"].called
