"""Serveur MCP de réservation de vols ENVIRECO (Duffel + Airtable)."""

import logging
import re
from datetime import date
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AuthProvider
from pydantic import BaseModel, EmailStr, Field, model_validator
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .airtable import (
    CHAMPS_PASSAGER,
    AirtableClient,
    AirtableError,
    echapper_formule,
)
from .config import Config, load_config
from .duffel import DuffelClient, DuffelError
from .formatting import resumer_commande, resumer_offre
from .instructions import INSTRUCTIONS
from .recherche import Historique, RechercheError, rechercher
from .reservation import ReservationError, Reservations
from .site_reservation import SiteReservation

logger = logging.getLogger("serveur_mcp_vol")

CodeIATA = Annotated[
    str, Field(pattern=r"^[A-Za-z]{3}$", description="Code IATA à 3 lettres, ex. CDG")
]
Classe = Literal["economy", "premium_economy", "business", "first"]
TELEPHONE_E164 = r"^\+[1-9]\d{6,14}$"


class PassagerRecherche(BaseModel):
    """Soit {"type": "adult"}, soit {"age": <âge>} pour un mineur."""

    type: Literal["adult"] | None = None
    age: int | None = Field(None, ge=0, le=17)

    @model_validator(mode="after")
    def _un_seul_champ(self) -> "PassagerRecherche":
        if (self.type is None) == (self.age is None):
            raise ValueError('Indiquer soit {"type": "adult"}, soit {"age": <âge>}.')
        return self


class PassagerReservation(BaseModel):
    id: str = Field(description="Identifiant passager Duffel issu de l'offre choisie")
    title: Literal["mr", "mrs", "ms", "miss", "dr"]
    gender: Literal["m", "f"]
    given_name: str = Field(min_length=1)
    family_name: str = Field(min_length=1)
    born_on: date
    email: EmailStr
    phone_number: str = Field(pattern=TELEPHONE_E164)
    infant_passenger_id: str | None = Field(
        None, description="Sur un adulte : id du nourrisson qu'il accompagne"
    )


OfferId = Annotated[str, Field(pattern=r"^off_\w+$")]
PassagersReservation = Annotated[list[PassagerReservation], Field(min_length=1)]
MontantConfirme = Annotated[
    str, Field(description="Prix total validé par l'utilisateur, ex. \"245.30\"")
]
DeviseConfirmee = Annotated[
    str, Field(pattern=r"^[A-Za-z]{3}$", description="Devise validée, ex. EUR")
]


def create_server(
    duffel: DuffelClient,
    airtable: AirtableClient,
    table_reservations: str,
    table_passagers: str,
    base_url: str,
    mot_de_passe: str,
    secret: str,
    auth: AuthProvider | None = None,
) -> FastMCP:
    mcp = FastMCP(
        name="Réservation de vols ENVIRECO", instructions=INSTRUCTIONS, auth=auth
    )
    reservations = Reservations(duffel, airtable, table_reservations)
    historique = Historique()
    site = SiteReservation(
        duffel,
        airtable,
        table_passagers,
        reservations,
        historique,
        base_url,
        mot_de_passe,
        secret,
    )

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    for route in site.routes():
        mcp.custom_route(route.path, methods=list(route.methods or []))(route.endpoint)

    @mcp.tool(
        annotations={
            "title": "Rechercher des vols",
            "readOnlyHint": True,
            "openWorldHint": True,
        }
    )
    async def rechercher_vols(
        code_iata_depart: CodeIATA,
        code_iata_arrivee: CodeIATA,
        date_aller: date,
        passagers: Annotated[
            list[PassagerRecherche],
            Field(
                min_length=1,
                max_length=9,
                description='Un élément par passager : {"type": "adult"} (16 ans ou '
                'plus) ou {"age": <âge>} pour un mineur.',
            ),
        ],
        date_retour: Annotated[
            date | None, Field(description="À renseigner pour un aller-retour")
        ] = None,
        classe: Classe = "economy",
        nombre_max_offres: Annotated[int, Field(ge=1, le=30)] = 10,
    ) -> dict[str, Any]:
        """Recherche des offres de vols (aller simple ou aller-retour).

        Renvoie les offres les moins chères, numérotées (1 = la moins chère),
        avec pour chacune son offer_id, son prix, sa date d'expiration, les
        identifiants passagers et le détail de chaque trajet. Ne réserve rien.
        Les offres sont aussi proposées, avec les mêmes numéros, sur la page de
        réservation ENVIRECO où l'utilisateur finalise lui-même sa réservation.
        """
        try:
            recherche = await rechercher(
                duffel,
                historique,
                code_iata_depart,
                code_iata_arrivee,
                date_aller,
                date_retour,
                [p.model_dump(exclude_none=True) for p in passagers],
                classe,
                nombre_max_offres,
            )
        except RechercheError as e:
            raise ToolError(e.message) from e
        return {
            "nombre_offres_trouvees": recherche.nombre_trouvees,
            "offres": [
                {"numero": numero, **resumer_offre(o)}
                for numero, o in enumerate(recherche.offres, start=1)
            ],
        }

    @mcp.tool(
        annotations={
            "title": "Réserver et payer un vol",
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        }
    )
    async def reserver_vol(
        offer_id: OfferId,
        passagers: PassagersReservation,
        montant_confirme: MontantConfirme,
        devise_confirmee: DeviseConfirmee,
    ) -> dict[str, Any]:
        """Réserve l'offre et la PAIE immédiatement (paiement réel et irréversible).

        À n'appeler qu'après la validation explicite du montant par l'utilisateur.
        Le serveur refuse si le prix de l'offre ne correspond plus au montant
        confirmé. La réservation est ensuite enregistrée dans la table Airtable
        reservation_vol. Ne jamais relancer cet outil après une erreur.
        Si cet outil n'est pas disponible, l'utilisateur peut finaliser sur la
        page de réservation ENVIRECO.
        """
        try:
            return await reservations.reserver(
                offer_id,
                [p.model_dump(mode="json", exclude_none=True) for p in passagers],
                montant_confirme,
                devise_confirmee,
            )
        except ReservationError as e:
            raise ToolError(e.message) from e

    @mcp.tool(
        annotations={
            "title": "Enregistrer une réservation dans Airtable",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
        }
    )
    async def enregistrer_reservation(
        order_id: Annotated[str, Field(pattern=r"^ord_\w+$")],
    ) -> dict[str, Any]:
        """Enregistre dans reservation_vol une réservation Duffel déjà effectuée.

        À utiliser uniquement si reserver_vol a renvoyé enregistre_dans_airtable
        à false. Ne crée aucun doublon et ne déclenche aucun paiement.
        """
        try:
            commande = await duffel.lire_commande(order_id)
        except DuffelError as e:
            raise ToolError(f"Commande introuvable : {e.message}") from e
        reference = commande.get("booking_reference") or ""
        try:
            existantes = await airtable.lister(
                table_reservations,
                formule=f"{{Référence_réservation}}='{echapper_formule(reference)}'",
            )
        except AirtableError as e:
            raise ToolError(e.message) from e
        if existantes:
            return {
                **resumer_commande(commande),
                "enregistre_dans_airtable": True,
                "deja_enregistree": True,
            }
        return {**resumer_commande(commande), **await reservations.enregistrer(commande)}

    @mcp.tool(
        annotations={"title": "Consulter les réservations", "readOnlyHint": True}
    )
    async def consulter_reservations(
        mois: Annotated[
            int, Field(ge=1, le=24, description="Nombre de mois à venir à couvrir")
        ] = 3,
    ) -> dict[str, Any]:
        """Liste les vols déjà réservés (table reservation_vol) sur les mois à venir.

        Les dates sont des heures locales de l'aéroport de départ.
        """
        formule = (
            "AND(IS_AFTER({Date}, DATEADD(NOW(), -1, 'days')), "
            f"IS_BEFORE({{Date}}, DATEADD(NOW(), {mois}, 'months')))"
        )
        try:
            lignes = await airtable.lister(table_reservations, formule=formule, tri="Date")
        except AirtableError as e:
            raise ToolError(e.message) from e
        for ligne in lignes:
            if isinstance(ligne.get("Date"), str):
                ligne["Date"] = re.sub(r"(\.\d+)?Z$", "", ligne["Date"])
        return {"periode_mois": mois, "reservations": lignes}

    @mcp.tool(annotations={"title": "Lister les passagers", "readOnlyHint": True})
    async def lister_passagers(
        prenom: Annotated[
            str | None, Field(description="Filtre optionnel sur le prénom")
        ] = None,
    ) -> dict[str, Any]:
        """Lit les coordonnées des passagers (table Coordonnees_passager_vol)."""
        formule = None
        if prenom and prenom.strip():
            formule = (
                f"FIND(LOWER('{echapper_formule(prenom.strip())}'), LOWER({{Prénom}}))"
            )
        try:
            lignes = await airtable.lister(table_passagers, formule=formule)
        except AirtableError as e:
            raise ToolError(e.message) from e
        return {
            "passagers": [
                {c: ligne.get(c) for c in CHAMPS_PASSAGER} for ligne in lignes
            ]
        }

    @mcp.tool(
        annotations={
            "title": "Ajouter un passager",
            "readOnlyHint": False,
            "destructiveHint": False,
        }
    )
    async def ajouter_passager(
        nom: Annotated[str, Field(min_length=1)],
        prenom: Annotated[str, Field(min_length=1)],
        titre: Literal["mr", "mrs", "ms", "miss"],
        genre: Literal["m", "f"],
        date_naissance: date,
        email: EmailStr,
        telephone: Annotated[
            str,
            Field(pattern=TELEPHONE_E164, description="Format international +336…"),
        ],
    ) -> dict[str, Any]:
        """Ajoute un passager dans la table Airtable Coordonnees_passager_vol.

        Refuse la création si un passager de mêmes nom, prénom et date de
        naissance existe déjà.
        """
        nom, prenom = nom.strip(), prenom.strip()
        if date_naissance >= date.today():
            raise ToolError("La date de naissance doit être dans le passé.")
        formule = (
            f"AND(LOWER({{Nom}})=LOWER('{echapper_formule(nom)}'), "
            f"LOWER({{Prénom}})=LOWER('{echapper_formule(prenom)}'))"
        )
        try:
            homonymes = await airtable.lister(table_passagers, formule=formule)
            if any(
                str(h.get("Date_naissance", ""))[:10] == date_naissance.isoformat()
                for h in homonymes
            ):
                raise ToolError(
                    f"{prenom} {nom} (né(e) le {date_naissance.isoformat()}) existe "
                    "déjà dans la table des passagers."
                )
            champs = {
                "Nom": nom,
                "Prénom": prenom,
                "Titre": titre,
                "Genre": genre,
                "Date_naissance": date_naissance.isoformat(),
                "Email": str(email),
                "Téléphone": telephone,
            }
            # typecast : accepte les valeurs si Titre/Genre sont des listes déroulantes.
            ids = await airtable.creer(table_passagers, [champs], typecast=True)
        except AirtableError as e:
            raise ToolError(e.message) from e
        return {"passager_ajoute": True, "id_airtable": ids[0], **champs}

    return mcp


def build(config: Config) -> FastMCP:
    from .auth import EnvirecoOAuthProvider

    return create_server(
        duffel=DuffelClient(config.duffel_api_key),
        airtable=AirtableClient(config.airtable_token, config.airtable_base_id),
        table_reservations=config.table_reservations,
        table_passagers=config.table_passagers,
        base_url=config.public_base_url,
        mot_de_passe=config.mcp_login_password,
        secret=config.jwt_secret,
        auth=EnvirecoOAuthProvider(config),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    config = load_config()
    logger.info(
        "Démarrage — Duffel en mode %s", "TEST" if config.duffel_mode_test else "LIVE"
    )
    build(config).run(
        transport="http",
        host="0.0.0.0",
        port=config.port,
        path="/mcp",
        stateless_http=True,
    )


if __name__ == "__main__":
    main()
