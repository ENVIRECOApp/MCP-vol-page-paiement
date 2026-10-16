"""Recherche d'offres Duffel et historique des dernières recherches.

L'historique, gardé en mémoire, permet à la page de réservation de proposer
les offres trouvées par l'assistant. Il est perdu au redémarrage du serveur :
la page permet alors de relancer une recherche.
"""

import secrets
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from .duffel import DuffelClient, DuffelError
from .reservation import expiration

TAILLE_HISTORIQUE = 10


class RechercheError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class Recherche:
    id: str
    horodatage: float
    depart: str
    arrivee: str
    date_aller: date
    date_retour: date | None
    passagers: list[dict[str, Any]]
    classe: str
    offres: list[dict[str, Any]]
    nombre_trouvees: int

    @property
    def description(self) -> str:
        trajet = f"{self.depart} → {self.arrivee} le {self.date_aller:%d/%m/%Y}"
        if self.date_retour:
            trajet += f", retour le {self.date_retour:%d/%m/%Y}"
        adultes = sum(1 for p in self.passagers if p.get("type") == "adult")
        enfants = [str(p["age"]) for p in self.passagers if "age" in p]
        composition = f"{adultes} adulte(s)"
        if enfants:
            composition += f", enfant(s) de {', '.join(enfants)} an(s)"
        return f"{trajet} — {composition} — {self.classe}"

    def offres_valides(self) -> list[tuple[int, dict[str, Any]]]:
        """Offres non expirées, avec leur numéro (1 = la moins chère)."""
        maintenant = datetime.now(timezone.utc)
        return [
            (numero, offre)
            for numero, offre in enumerate(self.offres, start=1)
            if (limite := expiration(offre)) is None or limite > maintenant
        ]


class Historique:
    def __init__(self) -> None:
        self._recherches: deque[Recherche] = deque(maxlen=TAILLE_HISTORIQUE)

    def ajouter(self, recherche: Recherche) -> None:
        self._recherches.appendleft(recherche)

    def recentes(self) -> list[Recherche]:
        return [r for r in self._recherches if r.offres_valides()]

    def trouver_offre(self, offer_id: str) -> tuple[Recherche, int, dict[str, Any]] | None:
        for recherche in self._recherches:
            for numero, offre in enumerate(recherche.offres, start=1):
                if offre.get("id") == offer_id:
                    return recherche, numero, offre
        return None


async def rechercher(
    duffel: DuffelClient,
    historique: Historique,
    depart: str,
    arrivee: str,
    date_aller: date,
    date_retour: date | None,
    passagers: list[dict[str, Any]],
    classe: str,
    nombre_max_offres: int,
) -> Recherche:
    """Interroge Duffel, trie les offres par prix et mémorise la recherche."""
    depart, arrivee = depart.upper(), arrivee.upper()
    if date_aller < date.today():
        raise RechercheError("La date aller est dans le passé.")
    if date_retour and date_retour < date_aller:
        raise RechercheError("La date de retour précède la date aller.")
    slices = [
        {"origin": depart, "destination": arrivee, "departure_date": date_aller.isoformat()}
    ]
    if date_retour:
        slices.append(
            {
                "origin": arrivee,
                "destination": depart,
                "departure_date": date_retour.isoformat(),
            }
        )
    try:
        resultat = await duffel.rechercher_offres(slices, passagers, classe)
    except DuffelError as e:
        raise RechercheError(f"Recherche Duffel impossible : {e.message}") from e

    offres = sorted(resultat.get("offers") or [], key=lambda o: Decimal(o["total_amount"]))
    recherche = Recherche(
        id=secrets.token_urlsafe(8),
        horodatage=time.time(),
        depart=depart,
        arrivee=arrivee,
        date_aller=date_aller,
        date_retour=date_retour,
        passagers=passagers,
        classe=classe,
        offres=offres[:nombre_max_offres],
        nombre_trouvees=len(offres),
    )
    historique.ajouter(recherche)
    return recherche
