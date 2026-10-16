"""Page web de réservation, utilisée hors de l'assistant.

L'assistant recherche les vols et conseille ; l'utilisateur ouvre lui-même
/reserver (protégée par MCP_LOGIN_PASSWORD), choisit l'offre et les passagers
de la base Airtable, relit le récapitulatif et paie.
"""

import asyncio
import hashlib
import hmac
import html
import re
import time
from datetime import date, datetime
from typing import Any

import jwt
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from .airtable import AirtableClient, AirtableError
from .auth import LimiteurEchecs, page_html
from .duffel import DuffelClient
from .formatting import resumer_offre, resumer_trajet
from .recherche import Historique, Recherche, RechercheError, rechercher
from .reservation import ReservationError, Reservations

COOKIE = "envireco_session"
DUREE_SESSION = 2 * 60 * 60
AGE_ADULTE = 16
AGE_NOURRISSON = 2
TELEPHONE_E164 = re.compile(r"^\+[1-9]\d{6,14}$")
CLASSES = {
    "economy": "Économique",
    "premium_economy": "Premium éco",
    "business": "Affaires",
    "first": "Première",
}
TITRES = {
    "mr": "mr", "m": "mr", "monsieur": "mr",
    "mrs": "mrs", "mme": "mrs", "madame": "mrs",
    "ms": "ms", "miss": "miss", "mlle": "miss", "mademoiselle": "miss",
    "dr": "dr",
}
GENRES = {
    "m": "m", "h": "m", "homme": "m", "masculin": "m", "male": "m",
    "f": "f", "femme": "f", "féminin": "f", "feminin": "f", "female": "f",
}


class SelectionError(Exception):
    pass


# --- Mise en forme ------------------------------------------------------------


def _e(valeur: Any) -> str:
    return html.escape("" if valeur is None else str(valeur))


def _date_lisible(valeur: str | None) -> str:
    if not valeur:
        return "?"
    try:
        return datetime.fromisoformat(valeur).strftime("%d/%m/%Y à %Hh%M")
    except ValueError:
        return valeur


def _trajet_html(trajet: dict[str, Any]) -> str:
    escales = trajet.get("nombre_escales") or 0
    details = [
        f"{_e(_date_lisible(trajet.get('heure_depart')))} → "
        f"{_e(_date_lisible(trajet.get('heure_arrivee')))}",
        f"{_e(', '.join(trajet.get('compagnies') or []))} "
        f"({_e(', '.join(trajet.get('vols') or []))})",
        f"{escales} escale(s)" if escales else "direct",
    ]
    if trajet.get("bagages_inclus"):
        details.append("bagages : " + _e(", ".join(trajet["bagages_inclus"])))
    if trajet.get("tarif"):
        details.append("tarif " + _e(trajet["tarif"]))
    return (
        f"✈️ <strong>{_e(trajet.get('depart'))} → {_e(trajet.get('arrivee'))}</strong>"
        f"<br>{' · '.join(details)}"
    )


def _recapitulatif(offre: dict[str, Any], noms: list[str]) -> str:
    lignes = [f"<li>{_trajet_html(t)}</li>" for t in resumer_offre(offre)["trajets"]]
    lignes.append(f"<li>👤 {_e(', '.join(noms))}</li>")
    lignes.append(
        f"<li>💶 <strong>{_e(offre.get('total_amount'))} "
        f"{_e(offre.get('total_currency'))}</strong></li>"
    )
    return "<ul>" + "".join(lignes) + "</ul>"


def _libelle_place(place: dict[str, Any], numero: int) -> str:
    age = place.get("age")
    if age is None:
        return f"Passager {numero} — adulte"
    return f"Passager {numero} — enfant de {age} an(s)"


def _nom_contact(contact: dict[str, Any]) -> str:
    return f"{contact.get('Prénom', '')} {contact.get('Nom', '')}".strip() or "(sans nom)"


# --- Conversion des contacts Airtable en passagers Duffel -----------------------


def _date_naissance(valeur: Any) -> date | None:
    texte = str(valeur or "").strip()
    for format_ in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(texte[:10], format_).date()
        except ValueError:
            continue
    return None


def _age(naissance: date, au: date) -> int:
    return au.year - naissance.year - ((au.month, au.day) < (naissance.month, naissance.day))


def _date_dernier_vol(offre: dict[str, Any]) -> date:
    for slice_ in reversed(offre.get("slices") or []):
        for segment in reversed(slice_.get("segments") or []):
            if segment.get("departing_at"):
                return datetime.fromisoformat(segment["departing_at"]).date()
    return date.today()


def contact_vers_passager(
    contact: dict[str, Any], place: dict[str, Any], date_vol: date
) -> dict[str, Any]:
    """Construit le passager Duffel ; lève SelectionError si la fiche est incomplète."""
    nom = _nom_contact(contact)

    def normaliser(champ: str, table: dict[str, str]) -> str:
        valeur = str(contact.get(champ) or "").strip().lower().rstrip(".")
        if valeur not in table:
            raise SelectionError(
                f"{nom} : champ « {champ} » absent ou non reconnu dans Airtable "
                f"(valeur : « {contact.get(champ) or ''} »)."
            )
        return table[valeur]

    naissance = _date_naissance(contact.get("Date_naissance"))
    if naissance is None:
        raise SelectionError(f"{nom} : date de naissance absente ou illisible dans Airtable.")
    telephone = re.sub(r"[\s.\-()]", "", str(contact.get("Téléphone") or ""))
    if telephone.startswith("00"):
        telephone = "+" + telephone[2:]
    if not TELEPHONE_E164.match(telephone):
        raise SelectionError(
            f"{nom} : téléphone à renseigner au format international (+33…) dans Airtable."
        )
    email = str(contact.get("Email") or "").strip()
    if "@" not in email:
        raise SelectionError(f"{nom} : adresse e-mail absente ou invalide dans Airtable.")
    if not contact.get("Prénom") or not contact.get("Nom"):
        raise SelectionError(f"{nom} : prénom ou nom manquant dans Airtable.")

    age = _age(naissance, date_vol)
    if place.get("age") is None and age < AGE_ADULTE:
        raise SelectionError(
            f"{nom} aura {age} ans au moment du vol : il ne peut pas occuper une place adulte."
        )
    if place.get("age") is not None and age >= AGE_ADULTE:
        raise SelectionError(
            f"{nom} aura {age} ans au moment du vol : il ne peut pas occuper une place enfant."
        )

    return {
        "id": place["id"],
        "title": normaliser("Titre", TITRES),
        "gender": normaliser("Genre", GENRES),
        "given_name": str(contact["Prénom"]).strip(),
        "family_name": str(contact["Nom"]).strip(),
        "born_on": naissance.isoformat(),
        "email": email,
        "phone_number": telephone,
    }


def construire_passagers(
    offre: dict[str, Any],
    choix: dict[str, str],
    contacts: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """choix : identifiant de place Duffel -> identifiant de contact Airtable."""
    places = offre.get("passengers") or []
    if any(not choix.get(p["id"]) for p in places):
        raise SelectionError("Choisissez un passager pour chaque place.")
    if len({choix[p["id"]] for p in places}) != len(places):
        raise SelectionError("Un même passager ne peut pas occuper deux places.")
    date_vol = _date_dernier_vol(offre)
    passagers = []
    for place in places:
        contact = contacts.get(choix[place["id"]])
        if contact is None:
            raise SelectionError("Passager introuvable dans Airtable : rechargez la page.")
        passagers.append(contact_vers_passager(contact, place, date_vol))

    # Chaque nourrisson voyage sur les genoux d'un adulte distinct.
    adultes = [p for place, p in zip(places, passagers) if place.get("age") is None]
    for place in places:
        if place.get("age") is not None and place["age"] < AGE_NOURRISSON:
            if not adultes:
                raise SelectionError("Chaque nourrisson doit être accompagné d'un adulte distinct.")
            adultes.pop(0)["infant_passenger_id"] = place["id"]
    return passagers


# --- Pages ------------------------------------------------------------------------


class SiteReservation:
    def __init__(
        self,
        duffel: DuffelClient,
        airtable: AirtableClient,
        table_passagers: str,
        reservations: Reservations,
        historique: Historique,
        base_url: str,
        mot_de_passe: str,
        secret: str,
    ):
        self._duffel = duffel
        self._airtable = airtable
        self._table_passagers = table_passagers
        self._reservations = reservations
        self._historique = historique
        self._securise = base_url.startswith("https://")
        self._mot_de_passe = mot_de_passe.encode()
        self._cle = hashlib.sha256(f"site|{secret}|{mot_de_passe}".encode()).digest()
        self._limiteur = LimiteurEchecs()
        self._verrous: dict[str, asyncio.Lock] = {}

    def routes(self) -> list[Route]:
        return [
            Route("/reserver", self.accueil, methods=["GET"]),
            Route("/reserver/connexion", self.connexion, methods=["POST"]),
            Route("/reserver/deconnexion", self.deconnexion, methods=["POST"]),
            Route("/reserver/recherche", self.nouvelle_recherche, methods=["POST"]),
            Route("/reserver/offre/{offer_id}", self.offre, methods=["GET", "POST"]),
        ]

    # Session --------------------------------------------------------------------

    def _connecte(self, request: Request) -> bool:
        jeton = request.cookies.get(COOKIE, "")
        try:
            return jwt.decode(jeton, self._cle, algorithms=["HS256"]).get("typ") == "site"
        except jwt.PyJWTError:
            return False

    @staticmethod
    def _retour_accueil() -> Response:
        return RedirectResponse("/reserver", status_code=303)

    def _page_connexion(self, erreur: str = "", status: int = 200) -> Response:
        return page_html(
            "<p>Saisissez le mot de passe du serveur pour accéder à la réservation.</p>"
            + (f'<p class="erreur">{_e(erreur)}</p>' if erreur else "")
            + '<form method="post" action="/reserver/connexion">'
            '<label for="password">Mot de passe</label>'
            '<input id="password" name="password" type="password" '
            'autocomplete="current-password" required autofocus>'
            '<button type="submit">Se connecter</button></form>',
            status,
            titre="Connexion",
        )

    async def connexion(self, request: Request) -> Response:
        formulaire = await request.form()
        ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
            request.client.host if request.client else "inconnue"
        )
        if self._limiteur.bloque(ip):
            return self._page_connexion("Trop de tentatives. Réessayez dans 15 minutes.", 429)
        if not hmac.compare_digest(
            str(formulaire.get("password", "")).encode(), self._mot_de_passe
        ):
            self._limiteur.enregistrer_echec(ip)
            return self._page_connexion("Mot de passe incorrect.", 401)
        maintenant = int(time.time())
        jeton = jwt.encode(
            {"typ": "site", "iat": maintenant, "exp": maintenant + DUREE_SESSION},
            self._cle,
            algorithm="HS256",
        )
        reponse = self._retour_accueil()
        reponse.set_cookie(
            COOKIE,
            jeton,
            max_age=DUREE_SESSION,
            path="/reserver",
            httponly=True,
            secure=self._securise,
            samesite="strict",
        )
        return reponse

    async def deconnexion(self, request: Request) -> Response:
        reponse = self._retour_accueil()
        reponse.delete_cookie(COOKIE, path="/reserver")
        return reponse

    # Accueil et recherche --------------------------------------------------------

    def _carte_recherche(self, recherche: Recherche) -> str:
        heure = datetime.fromtimestamp(recherche.horodatage).strftime("%Hh%M")
        cartes = []
        for numero, offre in recherche.offres_valides():
            trajets = "<br>".join(_trajet_html(t) for t in resumer_offre(offre)["trajets"])
            cartes.append(
                '<div class="carte">'
                f"<p><strong>Offre n°{numero} — {_e(offre.get('total_amount'))} "
                f"{_e(offre.get('total_currency'))}</strong></p><p>{trajets}</p>"
                f'<form method="get" action="/reserver/offre/{_e(offre.get("id"))}">'
                '<button type="submit">Choisir cette offre</button></form></div>'
            )
        return (
            f"<h2>{_e(recherche.description)} <small>(recherche de {heure})</small></h2>"
            + "".join(cartes)
        )

    def _formulaire_recherche(self) -> str:
        options = "".join(
            f'<option value="{code}">{libelle}</option>' for code, libelle in CLASSES.items()
        )
        return (
            "<h2>Nouvelle recherche</h2>"
            '<form method="post" action="/reserver/recherche"><div class="grille">'
            '<div><label for="depart">Départ (code IATA)</label>'
            '<input id="depart" name="depart" required maxlength="3" placeholder="CDG"></div>'
            '<div><label for="arrivee">Arrivée (code IATA)</label>'
            '<input id="arrivee" name="arrivee" required maxlength="3" placeholder="JFK"></div>'
            '<div><label for="date_aller">Date aller</label>'
            '<input id="date_aller" name="date_aller" type="date" required></div>'
            '<div><label for="date_retour">Date retour (facultatif)</label>'
            '<input id="date_retour" name="date_retour" type="date"></div>'
            '<div><label for="adultes">Adultes (16 ans et +)</label>'
            '<input id="adultes" name="adultes" type="number" min="0" max="9" value="1"></div>'
            '<div><label for="ages_enfants">Âges des enfants (ex. 14, 8)</label>'
            '<input id="ages_enfants" name="ages_enfants"></div>'
            f'<div><label for="classe">Classe</label><select id="classe" name="classe">{options}'
            "</select></div></div>"
            '<button type="submit">Rechercher</button></form>'
        )

    def _page_accueil(self, erreur: str = "", status: int = 200) -> Response:
        recherches = self._historique.recentes()
        if recherches:
            contenu = "".join(self._carte_recherche(r) for r in recherches)
        else:
            contenu = (
                "<p>Aucune offre en cours. Faites une recherche avec l'assistant puis "
                "rechargez cette page, ou utilisez le formulaire ci-dessous.</p>"
            )
        return page_html(
            "<p>Choisissez l'offre à réserver. Les numéros correspondent à ceux "
            "donnés par l'assistant (1 = la moins chère).</p>"
            + (f'<p class="erreur">{_e(erreur)}</p>' if erreur else "")
            + contenu
            + self._formulaire_recherche()
            + '<form method="post" action="/reserver/deconnexion">'
            '<button class="secondaire" type="submit">Se déconnecter</button></form>',
            status,
            titre="Réserver",
            large=True,
        )

    async def accueil(self, request: Request) -> Response:
        if not self._connecte(request):
            return self._page_connexion()
        return self._page_accueil()

    async def nouvelle_recherche(self, request: Request) -> Response:
        if not self._connecte(request):
            return self._retour_accueil()
        formulaire = await request.form()

        def champ(nom: str) -> str:
            return str(formulaire.get(nom, "")).strip()

        depart, arrivee, classe = champ("depart"), champ("arrivee"), champ("classe")
        if not (re.fullmatch(r"[A-Za-z]{3}", depart) and re.fullmatch(r"[A-Za-z]{3}", arrivee)):
            return self._page_accueil("Les aéroports doivent être des codes IATA à 3 lettres.", 400)
        if classe not in CLASSES:
            return self._page_accueil("Classe inconnue.", 400)
        try:
            date_aller = date.fromisoformat(champ("date_aller"))
            date_retour = date.fromisoformat(champ("date_retour")) if champ("date_retour") else None
            adultes = int(champ("adultes") or 0)
            ages = [int(a) for a in re.split(r"[,; ]+", champ("ages_enfants")) if a]
        except ValueError:
            return self._page_accueil("Dates, nombre d'adultes ou âges des enfants invalides.", 400)
        if not 0 <= adultes <= 9 or any(not 0 <= a < AGE_ADULTE for a in ages):
            return self._page_accueil(
                f"Nombre d'adultes ou âge d'enfant invalide (enfants : moins de {AGE_ADULTE} ans).",
                400,
            )
        passagers = [{"type": "adult"}] * adultes + [{"age": a} for a in ages]
        if not 1 <= len(passagers) <= 9:
            return self._page_accueil("Indiquez entre 1 et 9 passagers.", 400)
        try:
            await rechercher(
                self._duffel,
                self._historique,
                depart,
                arrivee,
                date_aller,
                date_retour,
                passagers,
                classe,
                nombre_max_offres=10,
            )
        except RechercheError as e:
            return self._page_accueil(e.message, 400)
        return self._retour_accueil()

    # Offre : choix des passagers, récapitulatif, paiement ---------------------------

    def _page_erreur(self, message: str, status: int, titre: str = "Réservation") -> Response:
        return page_html(
            f'<p class="erreur">{_e(message)}</p><p><a href="/reserver">Retour aux offres</a></p>',
            status,
            titre=titre,
        )

    def _page_selection(
        self,
        offre: dict[str, Any],
        numero: int,
        contacts: list[dict[str, Any]],
        choix: dict[str, str],
        erreur: str = "",
        status: int = 200,
    ) -> Response:
        champs = []
        for i, place in enumerate(offre.get("passengers") or [], start=1):
            options = ['<option value="">— choisir —</option>'] + [
                '<option value="{id}"{sel}>{nom} (né(e) le {naissance})</option>'.format(
                    id=_e(c["_id"]),
                    sel=" selected" if choix.get(place["id"]) == c["_id"] else "",
                    nom=_e(_nom_contact(c)),
                    naissance=_e(c.get("Date_naissance") or "?"),
                )
                for c in contacts
            ]
            champs.append(
                f'<label for="p{i}">{_e(_libelle_place(place, i))}</label>'
                f'<select id="p{i}" name="passager_{_e(place["id"])}" required>'
                + "".join(options)
                + "</select>"
            )
        return page_html(
            f"<h2>Offre n°{numero}</h2>"
            + _recapitulatif(offre, [])
            + (f'<p class="erreur">{_e(erreur)}</p>' if erreur else "")
            + f'<form method="post" action="/reserver/offre/{_e(offre["id"])}">'
            '<input type="hidden" name="action" value="verifier">'
            + "".join(champs)
            + '<button type="submit">Vérifier la réservation</button></form>'
            '<p><a href="/reserver">Retour aux offres</a></p>',
            status,
            titre="Passagers",
            large=True,
        )

    def _page_recapitulatif(
        self, offre: dict[str, Any], choix: dict[str, str], passagers: list[dict[str, Any]]
    ) -> Response:
        caches = "".join(
            f'<input type="hidden" name="passager_{_e(place_id)}" value="{_e(contact_id)}">'
            for place_id, contact_id in choix.items()
        )
        noms = [f"{p['given_name']} {p['family_name']}" for p in passagers]
        return page_html(
            "<p>Vérifiez la réservation. Le paiement est "
            "<strong>immédiat et irréversible</strong>.</p>"
            + _recapitulatif(offre, noms)
            + f'<form method="post" action="/reserver/offre/{_e(offre["id"])}">'
            '<input type="hidden" name="action" value="payer">'
            + caches
            + f'<button type="submit">Payer {_e(offre["total_amount"])} '
            f'{_e(offre["total_currency"])}</button></form>'
            f'<p><a href="/reserver/offre/{_e(offre["id"])}">Modifier les passagers</a></p>',
            titre="Récapitulatif",
            large=True,
        )

    @staticmethod
    def _page_confirmation(offre: dict[str, Any], resultat: dict[str, Any]) -> Response:
        if resultat.get("paiement_effectue") and resultat.get("reference_reservation"):
            entete = '<p class="succes">✅ Réservation confirmée et payée.</p>'
        else:
            entete = (
                '<p class="erreur">La commande a été créée mais le paiement n\'est pas '
                "confirmé. Vérifiez-la dans le tableau de bord Duffel.</p>"
            )
        details = [
            f"📋 Référence de réservation : <strong>{_e(resultat.get('reference_reservation'))}</strong>",
            "🎫 Billets : " + _e(", ".join(resultat.get("references_tickets") or []) or "aucun"),
        ]
        if resultat.get("enregistre_dans_airtable") is False:
            details.append(
                "⚠️ Réservation absente d'Airtable : demandez à l'assistant de "
                f"l'enregistrer (commande {_e(resultat.get('order_id'))})."
            )
        return page_html(
            entete
            + _recapitulatif(offre, resultat.get("passagers") or [])
            + "<ul>"
            + "".join(f"<li>{d}</li>" for d in details)
            + '</ul><p><a href="/reserver">Retour aux offres</a></p>',
            titre="Réservation confirmée",
            large=True,
        )

    async def offre(self, request: Request) -> Response:
        if not self._connecte(request):
            return self._retour_accueil()
        trouve = self._historique.trouver_offre(request.path_params["offer_id"])
        if trouve is None:
            return self._page_erreur(
                "Offre introuvable : elle a expiré ou le serveur a redémarré. "
                "Relancez une recherche. Rien n'a été payé.",
                404,
            )
        _, numero, offre = trouve
        try:
            liste = await self._airtable.lister(self._table_passagers, avec_id=True)
        except AirtableError as e:
            return self._page_erreur(e.message, 502)
        liste.sort(key=lambda c: _nom_contact(c).lower())
        contacts = {c["_id"]: c for c in liste}

        if request.method == "GET":
            return self._page_selection(offre, numero, liste, {})

        formulaire = await request.form()
        choix = {
            place["id"]: str(formulaire.get(f"passager_{place['id']}", ""))
            for place in offre.get("passengers") or []
        }
        try:
            passagers = construire_passagers(offre, choix, contacts)
        except SelectionError as e:
            return self._page_selection(offre, numero, liste, choix, str(e), 400)

        if formulaire.get("action") != "payer":
            return self._page_recapitulatif(offre, choix, passagers)

        # Un double clic attend la fin du premier paiement et affiche son résultat.
        verrou = self._verrous.setdefault(offre["id"], asyncio.Lock())
        async with verrou:
            try:
                resultat = await self._reservations.reserver(
                    offre["id"], passagers, offre["total_amount"], offre["total_currency"]
                )
            except ReservationError as e:
                return self._page_erreur(
                    e.message + " Relancez une recherche si besoin.", 409, "Paiement impossible"
                )
        return self._page_confirmation(offre, resultat)
