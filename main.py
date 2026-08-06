import os
import xmlrpc.client
import logging
import secrets
import re
import base64
from fastapi import FastAPI, Form, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from typing import Optional

logging.basicConfig(level=logging.INFO)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ODOO_URL      = os.getenv("ODOO_URL", "https://peb-pulls.odoo.com")
ODOO_DB       = os.getenv("ODOO_DB", "peb-pulls")
ODOO_USER     = os.getenv("ODOO_USER", "armine.sotodeh10@gmail.com")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD", "")
EXPERT_NAME   = "Armine Sotodeh"
RAILWAY_URL   = os.getenv("RAILWAY_URL", "https://web-production-5789.up.railway.app")


def odoo_connect():
    logging.info(f"Connexion Odoo: {ODOO_URL} / {ODOO_DB} / {ODOO_USER}")
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    logging.info(f"UID obtenu: {uid}")
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


class RefuseRequest(BaseModel):
    remarques: Optional[str] = ""


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
        logging.info(f"taken_slots: {len(events)} événements trouvés")
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
        logging.info(f"submit_rdv reçu: {req.prenom} {req.nom} / {req.email}")
        uid, models = odoo_connect()

        def find_or_create_partner(email, name, phone=""):
            logging.info(f"find_or_create_partner: {email}")
            existing = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "res.partner", "search_read",
                [[["email", "=", email]]],
                {"fields": ["id"], "limit": 1}
            )
            if existing:
                logging.info(f"Partenaire existant: {existing[0]['id']}")
                return existing[0]["id"]
            new_id = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "res.partner", "create",
                [{"name": name, "email": email, "phone": phone}]
            )
            logging.info(f"Nouveau partenaire créé: {new_id}")
            return new_id

        expert_id = find_or_create_partner(ODOO_USER, EXPERT_NAME)
        client_id = find_or_create_partner(req.email, f"{req.prenom} {req.nom}", req.tel)

        place_id = None
        if req.contact_place_email:
            place_id = find_or_create_partner(
                req.contact_place_email,
                req.contact_place_nom or req.contact_place_email,
                req.contact_place_tel
            )

        adresse = req.rue
        if req.boite:
            adresse += f", {req.boite}"
        adresse += f", {req.cp} {req.ville}"

        gestion = req.gestion_type or ""
        x_infos = "AGENCE\n" if gestion == "Agence" else "PROPRIÉTAIRE\n"
        if req.contact_place_nom:   x_infos += f"Nom : {req.contact_place_nom}\n"
        if req.contact_place_tel:   x_infos += f"Tél : {req.contact_place_tel}\n"
        if req.contact_place_email: x_infos += f"Email : {req.contact_place_email}\n"
        x_infos += f"\nBIEN\nType : {req.type_bien}\nSuperficie : {req.superficie}"
        x_infos += f"\n\nMANDATAIRE\nNom : {req.prenom} {req.nom}\nTél : {req.tel}\nEmail : {req.email}"

        title = f"PEB — {req.type_bien} {req.superficie} — {adresse}"
        logging.info(f"Création événement: {title}")

        partner_ids = [expert_id]
        if client_id and client_id != expert_id:
            partner_ids.append(client_id)
        if place_id and place_id not in partner_ids:
            partner_ids.append(place_id)

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
        logging.info(f"Événement créé: {event_id}")

        try:
            models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "calendar.event", "write",
                [[event_id], {
                    "x_studio_adresse_du_bien": adresse,
                    "x_studio_informations_sur_le_bien": x_infos
                }]
            )
            logging.info("Champs studio écrits")
        except Exception as e:
            logging.warning(f"Champs studio event: {e}")

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
                try:
                    models.execute_kw(
                        ODOO_DB, uid, ODOO_PASSWORD,
                        "calendar.event", "write",
                        [[event_id], {"x_studio_facture_liee": invoice_id}]
                    )
                except Exception as e:
                    logging.warning(f"Lien facture-event: {e}")
            except Exception as e:
                logging.warning(f"Création facture: {e}")

        return {"success": True, "event_id": event_id, "invoice_id": invoice_id}

    except Exception as e:
        logging.error(f"submit_rdv error: {e}")
        return {"success": False, "error": str(e)}


# ── DASHBOARD / WORKFLOW CLIENT ────────────────────────────

@app.post("/pebepc/dashboard/send_draft")
async def send_draft(
    event_id: int = Form(...),
    pdf: UploadFile = File(...)
):
    try:
        uid, models = odoo_connect()
        token = secrets.token_urlsafe(24)
        logging.info(f"send_draft: event_id={event_id}, token={token}, fichier={pdf.filename}")

        pdf_bytes = await pdf.read()
        pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")

        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "ir.attachment", "create",
            [{
                "name": pdf.filename or "PEB_provisoire.pdf",
                "type": "binary",
                "datas": pdf_b64,
                "res_model": "calendar.event",
                "res_id": event_id,
                "mimetype": "application/pdf"
            }]
        )
        logging.info("PDF attaché à l'événement Odoo")

        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "calendar.event", "write",
            [[event_id], {
                "x_studio_client_token": token,
                "x_studio_statut_draft": "draft_sent"
            }]
        )

        return {"success": True, "token": token}

    except Exception as e:
        logging.error(f"send_draft error: {e}")
        return {"success": False, "error": str(e)}


@app.get("/pebepc/mission/{token}/pdf")
def get_pdf(token: str):
    try:
        uid, models = odoo_connect()

        events = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "calendar.event", "search_read",
            [[["x_studio_client_token", "=", token], ["active", "=", True]]],
            {"fields": ["id"], "limit": 1}
        )
        if not events:
            return Response(content=b"Mission introuvable", status_code=404)

        event_id = events[0]["id"]

        # Trouver l'ID de l'attachment
        attachment_ids = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "ir.attachment", "search",
            [[
                ["res_model", "=", "calendar.event"],
                ["res_id", "=", event_id],
                ["mimetype", "=", "application/pdf"]
            ]],
            {"limit": 1, "order": "id desc"}
        )
        if not attachment_ids:
            return Response(content=b"PDF introuvable", status_code=404)

        # Lire via read() avec db_datas
        attachments = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "ir.attachment", "read",
            [attachment_ids],
            {"fields": ["name", "db_datas"]}
        )
        if not attachments or not attachments[0].get("db_datas"):
            return Response(content=b"PDF vide", status_code=404)

        pdf_bytes = base64.b64decode(attachments[0]["db_datas"])
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f"inline; filename=\"{attachments[0]['name']}\""}
        )

    except Exception as e:
        logging.error(f"get_pdf error: {e}")
        return Response(content=str(e).encode(), status_code=500)


@app.get("/pebepc/mission/{token}")
def get_mission(token: str):
    try:
        uid, models = odoo_connect()
        events = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "calendar.event", "search_read",
            [[["x_studio_client_token", "=", token], ["active", "=", True]]],
            {
                "fields": [
                    "id", "name", "start",
                    "x_studio_adresse_du_bien",
                    "x_studio_informations_sur_le_bien",
                    "x_studio_statut_draft",
                    "x_studio_remarques_client"
                ],
                "limit": 1
            }
        )
        if not events:
            return {"success": False, "error": "Mission introuvable"}

        ev = events[0]
        infos = ev.get("x_studio_informations_sur_le_bien", "") or ""
        type_bien, superficie, client_nom = "", "", ""

        tm = re.search(r"Type\s*:\s*(.+)", infos)
        if tm: type_bien = tm.group(1).strip()

        sm = re.search(r"Superficie\s*:\s*(.+)", infos)
        if sm: superficie = sm.group(1).strip()

        mand = infos.split("MANDATAIRE")
        if len(mand) > 1:
            nm = re.search(r"Nom\s*:\s*(.+)", mand[1])
            if nm: client_nom = nm.group(1).strip()

        statut = ev.get("x_studio_statut_draft") or "false"

        return {
            "success": True,
            "mission": {
                "id":         ev["id"],
                "name":       ev["name"],
                "start":      ev.get("start", ""),
                "adresse":    ev.get("x_studio_adresse_du_bien", ""),
                "type_bien":  type_bien,
                "superficie": superficie,
                "client_nom": client_nom,
                "statut":     str(statut),
                "remarques":  ev.get("x_studio_remarques_client", "") or ""
            }
        }

    except Exception as e:
        logging.error(f"get_mission error: {e}")
        return {"success": False, "error": str(e)}


@app.post("/pebepc/mission/{token}/accept")
def accept_mission(token: str):
    try:
        uid, models = odoo_connect()
        events = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "calendar.event", "search_read",
            [[["x_studio_client_token", "=", token], ["active", "=", True]]],
            {"fields": ["id"], "limit": 1}
        )
        if not events:
            return {"success": False, "error": "Mission introuvable"}

        event_id = events[0]["id"]
        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "calendar.event", "write",
            [[event_id], {"x_studio_statut_draft": "draft_accepted"}]
        )
        return {"success": True}

    except Exception as e:
        logging.error(f"accept_mission error: {e}")
        return {"success": False, "error": str(e)}


@app.post("/pebepc/mission/{token}/refuse")
def refuse_mission(token: str, req: RefuseRequest):
    try:
        uid, models = odoo_connect()
        events = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "calendar.event", "search_read",
            [[["x_studio_client_token", "=", token], ["active", "=", True]]],
            {"fields": ["id"], "limit": 1}
        )
        if not events:
            return {"success": False, "error": "Mission introuvable"}

        event_id = events[0]["id"]
        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "calendar.event", "write",
            [[event_id], {
                "x_studio_statut_draft":     "draft_refused",
                "x_studio_remarques_client": req.remarques or ""
            }]
        )
        return {"success": True}

    except Exception as e:
        logging.error(f"refuse_mission error: {e}")
        return {"success": False, "error": str(e)}
