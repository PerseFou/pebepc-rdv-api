import os
import xmlrpc.client
import logging
import secrets
import re
import base64
from fastapi import FastAPI, Form, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
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


def get_mission_info(uid, models, event_id):
    events = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "calendar.event", "read",
        [[event_id]],
        {"fields": ["name", "x_studio_informations_sur_le_bien", "x_studio_adresse_du_bien"]}
    )
    if not events:
        return {}
    ev = events[0]
    infos = ev.get("x_studio_informations_sur_le_bien", "") or ""

    client_nom, client_email, client_tel = "", "", ""
    mand = infos.split("MANDATAIRE")
    if len(mand) > 1:
        nm = re.search(r"Nom\s*:\s*(.+)", mand[1])
        if nm: client_nom = nm.group(1).strip()
        em = re.search(r"Email\s*:\s*(.+)", mand[1])
        if em: client_email = em.group(1).strip()
        pm = re.search(r"T.l\s*:\s*(.+)", mand[1])
        if pm: client_tel = pm.group(1).strip()

    return {
        "nom":     client_nom,
        "email":   client_email,
        "tel":     client_tel,
        "adresse": ev.get("x_studio_adresse_du_bien", ""),
        "name":    ev.get("name", "")
    }


def send_odoo_mail(uid, models, email_to, subject, body_html):
    try:
        mail_id = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "mail.mail", "create",
            [{
                "subject":    subject,
                "body_html":  body_html,
                "email_to":   email_to,
                "email_from": ODOO_USER,
                "auto_delete": True
            }]
        )
        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "mail.mail", "send",
            [[mail_id]]
        )
        logging.info(f"Mail envoyé à {email_to}")
        return True
    except Exception as e:
        logging.error(f"Erreur envoi mail: {e}")
        return False


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


# ── ENDPOINTS RDV ──────────────────────────────────────────

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
            [[["start", ">=", req.start], ["stop", "<=", req.stop], ["active", "=", True]]],
            {"fields": ["start", "stop"], "limit": 500}
        )
        logging.info(f"taken_slots: {len(events)} événements trouvés")
        return {"slots": [{"start": e["start"], "stop": e["stop"], "expertEmail": ODOO_USER} for e in (events or [])]}
    except Exception as e:
        logging.error(f"taken_slots error: {e}")
        return {"slots": []}


@app.post("/pebepc/rdv/submit")
def submit_rdv(req: SubmitRequest):
    try:
        logging.info(f"submit_rdv reçu: {req.prenom} {req.nom} / {req.email}")
        uid, models = odoo_connect()

        def find_or_create_partner(email, name, phone=""):
            existing = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "search_read", [[["email", "=", email]]], {"fields": ["id"], "limit": 1})
            if existing: return existing[0]["id"]
            return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "create", [{"name": name, "email": email, "phone": phone}])

        expert_id = find_or_create_partner(ODOO_USER, EXPERT_NAME)
        client_id = find_or_create_partner(req.email, f"{req.prenom} {req.nom}", req.tel)
        place_id = None
        if req.contact_place_email:
            place_id = find_or_create_partner(req.contact_place_email, req.contact_place_nom or req.contact_place_email, req.contact_place_tel)

        adresse = req.rue + (f", {req.boite}" if req.boite else "") + f", {req.cp} {req.ville}"

        gestion = req.gestion_type or ""
        x_infos = "AGENCE\n" if gestion == "Agence" else "PROPRIÉTAIRE\n"
        if req.contact_place_nom:   x_infos += f"Nom : {req.contact_place_nom}\n"
        if req.contact_place_tel:   x_infos += f"Tél : {req.contact_place_tel}\n"
        if req.contact_place_email: x_infos += f"Email : {req.contact_place_email}\n"
        x_infos += f"\nBIEN\nType : {req.type_bien}\nSuperficie : {req.superficie}"
        x_infos += f"\n\nMANDATAIRE\nNom : {req.prenom} {req.nom}\nTél : {req.tel}\nEmail : {req.email}"

        partner_ids = [expert_id]
        if client_id and client_id != expert_id: partner_ids.append(client_id)
        if place_id and place_id not in partner_ids: partner_ids.append(place_id)

        event_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "calendar.event", "create", [{
            "name": f"PEB — {req.type_bien} {req.superficie} — {adresse}",
            "start": req.start_utc, "stop": req.stop_utc,
            "description": f"Créneau : {req.creneau_label}\nAdresse : {adresse}\n\n{x_infos}",
            "partner_ids": [[6, 0, partner_ids]]
        }])

        try:
            models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "calendar.event", "write", [[event_id], {
                "x_studio_adresse_du_bien": adresse,
                "x_studio_informations_sur_le_bien": x_infos
            }])
        except Exception as e:
            logging.warning(f"Champs studio event: {e}")

        invoice_id = None
        if req.prix and req.prix > 0:
            try:
                htva = round(req.prix / 1.21, 2)
                taxes = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "account.tax", "search_read",
                    [[["amount", "=", 21], ["type_tax_use", "=", "sale"], ["active", "=", True]]], {"fields": ["id"], "limit": 1})
                tax_ids = [[4, taxes[0]["id"]]] if taxes else []
                invoice_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "account.move", "create", [{
                    "move_type": "out_invoice", "partner_id": client_id,
                    "invoice_line_ids": [[0, 0, {
                        "name": f"Certification PEB — {req.type_bien} {req.superficie} — {adresse}",
                        "quantity": 1, "price_unit": htva, "tax_ids": tax_ids
                    }]]
                }])
                models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "calendar.event", "write", [[event_id], {"x_studio_facture_liee": invoice_id}])
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
    """
    1. Lit le PDF uploadé
    2. Encode en base64 et le stocke dans x_studio_pdf_provisoire via XML-RPC
    3. Génère un token unique
    4. Met le statut à draft_sent
    5. Envoie un mail au mandataire avec le lien
    """
    try:
        uid, models = odoo_connect()
        token = secrets.token_urlsafe(24)
        logging.info(f"send_draft: event_id={event_id}, token={token}, fichier={pdf.filename}")

        pdf_bytes = await pdf.read()
        pdf_b64   = base64.b64encode(pdf_bytes).decode("utf-8")
        logging.info(f"PDF lu: {len(pdf_bytes)} octets")

        # ✅ Stocker le PDF en base64 dans Odoo via XML-RPC (pas de HTTP)
        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "calendar.event", "write",
            [[event_id], {
                "x_studio_client_token":   token,
                "x_studio_statut_draft":   "draft_sent",
                "x_studio_pdf_provisoire": pdf_b64
            }]
        )
        logging.info("PDF provisoire + token écrits dans Odoo")

        # Récupérer infos mandataire pour le mail
        infos        = get_mission_info(uid, models, event_id)
        client_email = infos.get("email", "")
        client_nom   = infos.get("nom", "le mandataire")
        adresse      = infos.get("adresse", "")
        client_link  = f"https://peb-pulls.odoo.com/rdv-client?token={token}"

        if client_email:
            subject   = "Votre PEB provisoire est disponible"
            body_html = f"""
<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
    <div style="background:linear-gradient(135deg,#1B3A8C,#3B82F6);padding:28px 32px;border-radius:12px 12px 0 0;">
        <h1 style="color:#fff;font-size:1.4rem;margin:0;">📄 Votre PEB provisoire est prêt</h1>
    </div>
    <div style="background:#fff;padding:28px 32px;border:1px solid #e5e7eb;border-radius:0 0 12px 12px;">
        <p style="color:#374151;">Bonjour <strong>{client_nom}</strong>,</p>
        <p style="color:#374151;">Votre certificat PEB provisoire pour le bien situé au :</p>
        <p style="background:#f4f6fb;padding:12px;border-radius:8px;color:#1B3A8C;font-weight:bold;">{adresse}</p>
        <p style="color:#374151;">est maintenant disponible. Veuillez le consulter et nous indiquer si vous l'acceptez ou souhaitez des modifications.</p>
        <div style="text-align:center;margin:28px 0;">
            <a href="{client_link}" style="background:#1B3A8C;color:#fff;padding:14px 32px;border-radius:999px;text-decoration:none;font-weight:bold;font-size:1rem;">
                Consulter mon PEB provisoire →
            </a>
        </div>
        <p style="color:#8a9bb5;font-size:0.82rem;">Si le bouton ne fonctionne pas, copiez ce lien :<br/>
        <a href="{client_link}" style="color:#1B3A8C;">{client_link}</a></p>
        <hr style="border:none;border-top:1px solid #e5e7eb;margin:20px 0;"/>
        <p style="color:#8a9bb5;font-size:0.78rem;">Armine Sotodeh — Expert PEB<br/>
        <a href="mailto:{ODOO_USER}" style="color:#1B3A8C;">{ODOO_USER}</a></p>
    </div>
</div>"""
            send_odoo_mail(uid, models, client_email, subject, body_html)
        else:
            logging.warning(f"Pas d'email trouvé pour event_id={event_id}")

        return {"success": True, "token": token, "mail_sent": bool(client_email)}

    except Exception as e:
        logging.error(f"send_draft error: {e}")
        return {"success": False, "error": str(e)}


@app.post("/pebepc/dashboard/send_final")
async def send_final(
    event_id: int = Form(...),
    pdf: UploadFile = File(...)
):
    try:
        uid, models = odoo_connect()
        logging.info(f"send_final: event_id={event_id}, fichier={pdf.filename}")

        pdf_bytes = await pdf.read()
        pdf_b64   = base64.b64encode(pdf_bytes).decode("utf-8")
        logging.info(f"PDF definitif lu: {len(pdf_bytes)} octets")

        # Stocker le PDF definitif via XML-RPC
        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "calendar.event", "write",
            [[event_id], {"x_studio_pdf_definitif": pdf_b64}]
        )
        logging.info("PDF definitif ecrit dans Odoo")

        # Statut closed separement
        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "calendar.event", "write",
            [[event_id], {"x_studio_statut_draft": "closed"}]
        )

        infos        = get_mission_info(uid, models, event_id)
        client_email = infos.get("email", "")
        client_nom   = infos.get("nom", "le mandataire")
        adresse      = infos.get("adresse", "")

        token_ev = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "calendar.event", "read",
            [[event_id]],
            {"fields": ["x_studio_client_token"]}
        )
        token    = token_ev[0].get("x_studio_client_token", "") if token_ev else ""
        pdf_link = f"https://peb-pulls.odoo.com/rdv-client?token={token}" if token else ""

        if client_email:
            subject   = "Votre certificat PEB definitif est disponible"
            body_html = f"""
<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
    <div style="background:linear-gradient(135deg,#059669,#10B981);padding:28px 32px;border-radius:12px 12px 0 0;">
        <h1 style="color:#fff;font-size:1.4rem;margin:0;">Votre certificat PEB definitif est pret</h1>
    </div>
    <div style="background:#fff;padding:28px 32px;border:1px solid #e5e7eb;border-radius:0 0 12px 12px;">
        <p style="color:#374151;">Bonjour <strong>{client_nom}</strong>,</p>
        <p style="color:#374151;">Votre certificat PEB <strong>definitif</strong> pour le bien situe au :</p>
        <p style="background:#f0fdf4;padding:12px;border-radius:8px;color:#16a34a;font-weight:bold;">{adresse}</p>
        <p style="color:#374151;">est maintenant disponible. Vous pouvez le telecharger en cliquant ci-dessous.</p>
        {"<div style='text-align:center;margin:28px 0;'><a href='" + pdf_link + "' style='background:#10B981;color:#fff;padding:14px 32px;border-radius:999px;text-decoration:none;font-weight:bold;font-size:1rem;'>Telecharger mon certificat PEB</a></div>" if pdf_link else ""}
        <hr style="border:none;border-top:1px solid #e5e7eb;margin:20px 0;"/>
        <p style="color:#8a9bb5;font-size:0.78rem;">Armine Sotodeh - Expert PEB<br/>
        <a href="mailto:{ODOO_USER}" style="color:#10B981;">{ODOO_USER}</a></p>
    </div>
</div>"""
            send_odoo_mail(uid, models, client_email, subject, body_html)

        return {"success": True, "mail_sent": bool(client_email)}

    except Exception as e:
        logging.error(f"send_final error: {e}")
        return {"success": False, "error": str(e)}


# ── SERVIR LE PDF DEPUIS ODOO VIA XML-RPC ─────────────────

@app.get("/pebepc/mission/{token}/pdf")
def get_pdf_provisoire(token: str):
    """
    Récupère le PDF provisoire depuis le champ x_studio_pdf_provisoire
    via XML-RPC (pas de HTTP vers Odoo — contourne le blocage SaaS).
    """
    try:
        uid, models = odoo_connect()

        events = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "calendar.event", "search_read",
            [[["x_studio_client_token", "=", token], ["active", "=", True]]],
            {"fields": ["id", "name", "x_studio_pdf_provisoire"], "limit": 1}
        )

        if not events:
            return Response(content=b"Mission introuvable", status_code=404)

        ev      = events[0]
        pdf_b64 = ev.get("x_studio_pdf_provisoire")

        if not pdf_b64:
            return Response(content=b"Aucun PDF provisoire disponible", status_code=404)

        pdf_bytes = base64.b64decode(pdf_b64)
        filename  = f"PEB_provisoire_{ev['id']}.pdf"

        logging.info(f"PDF provisoire servi: {len(pdf_bytes)} octets pour token={token}")
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f"inline; filename=\"{filename}\"",
                "Access-Control-Allow-Origin": "*"
            }
        )

    except Exception as e:
        logging.error(f"get_pdf_provisoire error: {e}")
        return Response(content=str(e).encode(), status_code=500)


@app.get("/pebepc/mission/{token}/pdf/final")
def get_pdf_definitif(token: str):
    """
    Récupère le PDF définitif depuis x_studio_pdf_definitif via XML-RPC.
    """
    try:
        uid, models = odoo_connect()

        events = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "calendar.event", "search_read",
            [[["x_studio_client_token", "=", token], ["active", "=", True]]],
            {"fields": ["id", "name", "x_studio_pdf_definitif"], "limit": 1}
        )

        if not events:
            return Response(content=b"Mission introuvable", status_code=404)

        ev      = events[0]
        pdf_b64 = ev.get("x_studio_pdf_definitif")

        if not pdf_b64:
            return Response(content="Aucun PDF definitif disponible".encode(), status_code=404)

        pdf_bytes = base64.b64decode(pdf_b64)
        filename  = f"PEB_definitif_{ev['id']}.pdf"

        logging.info(f"PDF définitif servi: {len(pdf_bytes)} octets pour token={token}")
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f"inline; filename=\"{filename}\"",
                "Access-Control-Allow-Origin": "*"
            }
        )

    except Exception as e:
        logging.error(f"get_pdf_definitif error: {e}")
        return Response(content=str(e).encode(), status_code=500)


# ── MISSION CLIENT ─────────────────────────────────────────

@app.get("/pebepc/mission/{token}")
def get_mission(token: str):
    try:
        uid, models = odoo_connect()
        events = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "calendar.event", "search_read",
            [[["x_studio_client_token", "=", token], ["active", "=", True]]],
            {"fields": ["id", "name", "start", "x_studio_adresse_du_bien",
                        "x_studio_informations_sur_le_bien", "x_studio_statut_draft",
                        "x_studio_remarques_client", "x_studio_pdf_provisoire"], "limit": 1}
        )

        if not events:
            return {"success": False, "error": "Mission introuvable"}

        ev    = events[0]
        infos = ev.get("x_studio_informations_sur_le_bien", "") or ""
        type_bien = superficie = client_nom = ""

        tm = re.search(r"Type\s*:\s*(.+)", infos)
        if tm: type_bien = tm.group(1).strip()
        sm = re.search(r"Superficie\s*:\s*(.+)", infos)
        if sm: superficie = sm.group(1).strip()
        mand = infos.split("MANDATAIRE")
        if len(mand) > 1:
            nm = re.search(r"Nom\s*:\s*(.+)", mand[1])
            if nm: client_nom = nm.group(1).strip()

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
                "statut":     str(ev.get("x_studio_statut_draft") or "false"),
                "remarques":  ev.get("x_studio_remarques_client", "") or "",
                "has_pdf":    bool(ev.get("x_studio_pdf_provisoire"))
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
        models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "calendar.event", "write",
            [[event_id], {"x_studio_statut_draft": "draft_accepted"}])
        logging.info(f"Mission {event_id} acceptée")
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
        models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "calendar.event", "write",
            [[event_id], {
                "x_studio_statut_draft":     "draft_refused",
                "x_studio_remarques_client": req.remarques or ""
            }])
        logging.info(f"Mission {event_id} refusée")
        return {"success": True}
    except Exception as e:
        logging.error(f"refuse_mission error: {e}")
        return {"success": False, "error": str(e)}
