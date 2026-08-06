import os
import xmlrpc.client
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

logging.basicConfig(level=logging.INFO)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restreindre à https://peb-pulls.odoo.com en production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ODOO_URL      = os.getenv("ODOO_URL", "https://peb-pulls.odoo.com")
ODOO_DB       = os.getenv("ODOO_DB", "peb-pulls")
ODOO_USER     = os.getenv("ODOO_USER", "armine.sotodeh10@gmail.com")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD", "")
EXPERT_NAME   = "Armine Sotodeh"


def odoo_connect():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        raise Exception("Authentification Odoo échouée")
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return uid, models


# ── MODÈLES ────────────────────────────────────────────────

class TakenSlotsRequest(BaseModel):
    start: str
    stop: str


class SubmitRequest(BaseModel):
    prenom: str
    nom: str
    email: str
    tel: str
    type_bien: str
    superficie: str
    prix: float = 0
    rue: str
    boite: Optional[str] = ""
    cp: str
    ville: str
    creneau_label: str
    start_utc: str
    stop_utc: str
    gestion_type: Optional[str] = ""
    contact_place_nom: Optional[str] = ""
    contact_place_email: Optional[str] = ""
    contact_place_tel: Optional[str] = ""
    fact_type: Optional[str] = "Particulier"
    fact_nom: Optional[str] = ""
    fact_addr: Optional[str] = ""
    fact_email: Optional[str] = ""
    fact_tva: Optional[str] = ""


# ── ENDPOINTS ──────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "ok", "service": "pebepc-rdv-api"}


@app.post("/pebepc/rdv/taken_slots")
def taken_slots(req: TakenSlotsRequest):
    try:
        uid, models = odoo_connect()
        events = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "calendar.event", "search_read",
            [[
                ["start", ">=", req.start],
                ["stop",  "<=", req.stop],
                ["active", "=", True]
            ]],
            {"fields": ["start", "stop"], "limit": 500}
        )
        slots = [
            {"start": e["start"], "stop": e["stop"], "expertEmail": ODOO_USER}
            for e in (events or [])
        ]
        return {"slots": slots}
    except Exception as e:
        logging.error(f"taken_slots error: {e}")
        return {"slots": []}


@app.post("/pebepc/rdv/submit")
def submit_rdv(req: SubmitRequest):
    try:
        uid, models = odoo_connect()

        def find_or_create_partner(email, name, phone=""):
            existing = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "res.partner", "search_read",
                [[["email", "=", email]]],
                {"fields": ["id"], "limit": 1}
            )
            if existing:
                return existing[0]["id"]
            return models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "res.partner", "create",
                [{"name": name, "email": email, "phone": phone}]
            )

        # Partenaires
        expert_id = find_or_create_partner(ODOO_USER, EXPERT_NAME)
        client_id = find_or_create_partner(req.email, f"{req.prenom} {req.nom}", req.tel)

        # Partenaire contact sur place
        place_id = None
        if req.contact_place_email:
            place_id = find_or_create_partner(
                req.contact_place_email,
                req.contact_place_nom or req.contact_place_email,
                req.contact_place_tel
            )

        # Adresse complète
        adresse = req.rue
        if req.boite:
            adresse += f", {req.boite}"
        adresse += f", {req.cp} {req.ville}"

        # Bloc informations structuré (champ Odoo)
        gestion = req.gestion_type or ""
        x_infos = "AGENCE\n" if gestion == "Agence" else "PROPRIÉTAIRE\n"
        if req.contact_place_nom:   x_infos += f"Nom : {req.contact_place_nom}\n"
        if req.contact_place_tel:   x_infos += f"Tél : {req.contact_place_tel}\n"
        if req.contact_place_email: x_infos += f"Email : {req.contact_place_email}\n"
        x_infos += f"\nBIEN\nType : {req.type_bien}\nSuperficie : {req.superficie}"
        x_infos += f"\n\nMANDATAIRE\nNom : {req.prenom} {req.nom}\nTél : {req.tel}\nEmail : {req.email}"

        title = f"PEB — {req.type_bien} {req.superficie} — {adresse}"

        # Partner IDs uniques
        partner_ids = list({expert_id, client_id})
        if place_id:
            partner_ids.append(place_id)

        # Création événement calendrier
        event_id = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "calendar.event", "create",
            [{
                "name": title,
                "start": req.start_utc,
                "stop": req.stop_utc,
                "description": f"Créneau : {req.creneau_label}\nAdresse : {adresse}\n\n{x_infos}",
                "partner_ids": [[6, 0, partner_ids]]
            }]
        )

        # Champs custom studio (best effort)
        try:
            models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "calendar.event", "write",
                [[event_id], {
                    "x_studio_adresse_du_bien": adresse,
                    "x_studio_informations_sur_le_bien": x_infos
                }]
            )
        except Exception as e:
            logging.warning(f"Champs studio event: {e}")

        # Création facture si prix > 0
        invoice_id = None
        if req.prix and req.prix > 0:
            try:
                htva = round(req.prix / 1.21, 2)
                taxes = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD,
                    "account.tax", "search_read",
                    [[["amount", "=", 21], ["type_tax_use", "=", "sale"], ["active", "=", True]]],
                    {"fields": ["id"], "limit": 1}
                )
                tax_ids = [[4, taxes[0]["id"]]] if taxes else []

                invoice_id = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD,
                    "account.move", "create",
                    [{
                        "move_type": "out_invoice",
                        "partner_id": client_id,
                        "invoice_line_ids": [[0, 0, {
                            "name": f"Certification PEB — {req.type_bien} {req.superficie} — {adresse}",
                            "quantity": 1,
                            "price_unit": htva,
                            "tax_ids": tax_ids
                        }]]
                    }]
                )

                # Lien facture ↔ événement (best effort)
                try:
                    models.execute_kw(
                        ODOO_DB, uid, ODOO_PASSWORD,
                        "calendar.event", "write",
                        [[event_id], {"x_invoice_id": invoice_id}]
                    )
                except Exception as e:
                    logging.warning(f"Lien facture-event: {e}")

            except Exception as e:
                logging.warning(f"Création facture: {e}")

        return {"success": True, "event_id": event_id, "invoice_id": invoice_id}

    except Exception as e:
        logging.error(f"submit_rdv error: {e}")
        return {"success": False, "error": str(e)}
