"""Réservation et paiement d'une offre Duffel, avec les garde-fous communs à
l'outil reserver_vol et à la page de paiement."""

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .airtable import AirtableClient
from .duffel import DuffelClient, DuffelError, DuffelIncertain
from .formatting import lignes_reservation, resumer_commande

logger = logging.getLogger("serveur_mcp_vol")


class ReservationError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def expiration(offre: dict[str, Any]) -> datetime | None:
    expire = offre.get("expires_at")
    if not expire:
        return None
    try:
        limite = datetime.fromisoformat(expire.replace("Z", "+00:00"))
    except ValueError:
        return None
    return limite if limite.tzinfo else limite.replace(tzinfo=timezone.utc)


class Reservations:
    def __init__(self, duffel: DuffelClient, airtable: AirtableClient, table_reservations: str):
        self._duffel = duffel
        self._airtable = airtable
        self._table = table_reservations
        # Garde-fous contre une double réservation (nouvel essai du client).
        self._en_cours: set[str] = set()
        self._incertaines: set[str] = set()
        self._reservees: dict[str, dict[str, Any]] = {}

    async def verifier(
        self,
        offer_id: str,
        passagers: list[dict[str, Any]],
        montant_confirme: str,
        devise_confirmee: str,
    ) -> dict[str, Any]:
        """Relit l'offre et vérifie prix, expiration et passagers. Ne paie rien."""
        try:
            montant = Decimal(montant_confirme.replace(",", ".").strip())
        except InvalidOperation as e:
            raise ReservationError("Le montant confirmé n'est pas un nombre valide.") from e
        devise = devise_confirmee.upper()

        try:
            offre = await self._duffel.lire_offre(offer_id)
        except DuffelError as e:
            raise ReservationError(
                f"Offre indisponible ({e.message}). Relancer une recherche."
            ) from e
        limite = expiration(offre)
        if limite is not None and limite <= datetime.now(timezone.utc):
            raise ReservationError("Cette offre a expiré. Relancer une recherche.")

        prix, devise_offre = offre["total_amount"], offre["total_currency"]
        if Decimal(prix) != montant or devise_offre.upper() != devise:
            raise ReservationError(
                f"Le prix actuel de l'offre est {prix} {devise_offre}, différent "
                f"du montant confirmé ({montant_confirme} {devise}). Rien n'a été "
                "réservé : demander une nouvelle confirmation à l'utilisateur."
            )

        attendus = {p["id"] for p in offre.get("passengers") or []}
        fournis = [p["id"] for p in passagers]
        if set(fournis) != attendus or len(fournis) != len(attendus):
            raise ReservationError(
                "Les identifiants passagers ne correspondent pas à ceux de "
                f"l'offre. Attendus : {sorted(attendus)}."
            )
        return offre

    async def reserver(
        self,
        offer_id: str,
        passagers: list[dict[str, Any]],
        montant_confirme: str,
        devise_confirmee: str,
    ) -> dict[str, Any]:
        """Réserve et paie l'offre, puis l'enregistre dans reservation_vol."""
        if offer_id in self._reservees:
            return {**self._reservees[offer_id], "deja_reservee": True}
        if offer_id in self._incertaines:
            raise ReservationError(
                "Une tentative précédente sur cette offre n'a pas reçu de réponse de "
                "Duffel : la réservation a peut-être abouti. Vérifier dans le tableau "
                "de bord Duffel avant toute nouvelle tentative."
            )
        if offer_id in self._en_cours:
            raise ReservationError("Une réservation de cette offre est déjà en cours.")

        self._en_cours.add(offer_id)
        try:
            offre = await self.verifier(
                offer_id, passagers, montant_confirme, devise_confirmee
            )
            try:
                commande = await self._duffel.creer_commande(
                    offer_id,
                    passagers,
                    offre["total_amount"],
                    offre["total_currency"],
                )
            except DuffelIncertain as e:
                self._incertaines.add(offer_id)
                raise ReservationError(
                    "Duffel n'a pas répondu à la demande de réservation : son "
                    "résultat est INCONNU (elle a peut-être abouti et été payée). "
                    "Ne pas relancer ; vérifier dans le tableau de bord Duffel."
                ) from e
            except DuffelError as e:
                raise ReservationError(
                    f"Réservation refusée par Duffel, rien n'a été payé : {e.message}"
                ) from e

            resultat = {**resumer_commande(commande), **await self.enregistrer(commande)}
            self._reservees[offer_id] = resultat
            return resultat
        finally:
            self._en_cours.discard(offer_id)

    async def enregistrer(self, commande: dict[str, Any]) -> dict[str, Any]:
        """Écrit la commande dans reservation_vol sans jamais lever d'exception."""
        try:
            ids = await self._airtable.creer(self._table, lignes_reservation(commande))
        except Exception as e:  # la réservation est payée : ne pas masquer le succès
            logger.exception("Écriture Airtable échouée pour %s", commande.get("id"))
            return {
                "enregistre_dans_airtable": False,
                "erreur_airtable": getattr(e, "message", str(e)),
            }
        return {"enregistre_dans_airtable": True, "lignes_airtable_creees": len(ids)}
