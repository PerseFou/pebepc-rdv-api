import os
import logging
import secrets
import re
import base64
from contextlib import asynccontextmanager
from fastapi import FastAPI, Form, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse
from pydantic import BaseModel
from typing import Optional

from odoo_client import odoo_connect, get_mission_info, ODOO_DB, ODOO_USER, ODOO_PASSWORD, EXPERT_NAME
from mcp_server import mcp

logging.basicConfig(level=logging.INFO)

MCP_API_KEY = os.getenv("MCP_API_KEY", "")
mcp_app = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with mcp.session_manager.run():
        yield


class MCPAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/mcp"):
            headers = dict(scope["headers"])
            auth = headers.get(b"authorization", b"").decode()
            if not MCP_API_KEY or auth != f"Bearer {MCP_API_KEY}":
                response = JSONResponse({"error": "unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(MCPAuthMiddleware)

RAILWAY_URL = os.getenv("RAILWAY_URL", "https://web-production-5789.up.railway.app")


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
        models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "mail.mail", "send", [[mail_id]])
        logging.info(f"Mail envoye a {email_to}")
        return True
    except Exception as e:
        logging.error(f"Erreur envoi mail: {e}")
        return False


# ── MODELES ────────────────────────────────────────────────

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


class ChatRequest(BaseModel):
    message: str


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
        logging.info(f"taken_slots: {len(events)} evenements trouves")
        return {"slots": [{"start": e["start"], "stop": e["stop"], "expertEmail": ODOO_USER} for e in (events or [])]}
    except Exception as e:
        logging.error(f"taken_slots error: {e}")
        return {"slots": []}


@app.post("/pebepc/rdv/submit")
def submit_rdv(req: SubmitRequest):
    try:
        logging.info(f"submit_rdv recu: {req.prenom} {req.nom} / {req.email}")
        uid, models = odoo_connect()

        def find_or_create_partner(email, name, phone=""):
            existing = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "search_read",
                [[["email", "=ilike", email.strip()]]],
                {"fields": ["id", "name", "phone"], "order": "id asc", "limit": 1})
            if existing:
                pid = existing[0]["id"]
                updates = {}
                if name and not existing[0].get("name"): updates["name"] = name
                if phone and not existing[0].get("phone"): updates["phone"] = phone
                if updates:
                    models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "write", [[pid], updates])
                return pid
            return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "create",
                [{"name": name, "email": email.strip().lower(), "phone": phone}])

        expert_id = find_or_create_partner(ODOO_USER, EXPERT_NAME)
        client_id = find_or_create_partner(req.email, f"{req.prenom} {req.nom}", req.tel)
        place_id = None
        if req.contact_place_email:
            place_id = find_or_create_partner(req.contact_place_email, req.contact_place_nom or req.contact_place_email, req.contact_place_tel)

        adresse = req.rue + (f", {req.boite}" if req.boite else "") + f", {req.cp} {req.ville}"

        gestion = req.gestion_type or ""
        x_infos = "AGENCE\n" if gestion == "Agence" else "PROPRIETAIRE\n"
        if req.contact_place_nom:   x_infos += f"Nom : {req.contact_place_nom}\n"
        if req.contact_place_tel:   x_infos += f"Tel : {req.contact_place_tel}\n"
        if req.contact_place_email: x_infos += f"Email : {req.contact_place_email}\n"
        x_infos += f"\nBIEN\nType : {req.type_bien}\nSuperficie : {req.superficie}"
        x_infos += f"\n\nMANDATAIRE\nNom : {req.prenom} {req.nom}\nTel : {req.tel}\nEmail : {req.email}"

        partner_ids = [expert_id]
        if client_id and client_id != expert_id: partner_ids.append(client_id)
        if place_id and place_id not in partner_ids: partner_ids.append(place_id)

        event_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "calendar.event", "create", [{
            "name": f"PEB — {req.type_bien} {req.superficie} — {adresse}",
            "start": req.start_utc, "stop": req.stop_utc,
            "description": f"Creneau : {req.creneau_label}\nAdresse : {adresse}\n\n{x_infos}",
            "partner_ids": [[6, 0, partner_ids]],
            "privacy": "confidential"
        }], {"context": {"no_mail_to_attendees": True}})

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
                logging.warning(f"Creation facture: {e}")

        # ── Mail notification Armine ──
        subject_expert = f"Nouveau RDV PEB - {req.prenom} {req.nom}"
        body_expert = f"""<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
    <div style="background:linear-gradient(135deg,#1B3A8C,#3B82F6);padding:24px 28px;border-radius:12px 12px 0 0;">
        <h1 style="color:#fff;font-size:1.2rem;margin:0;">Nouveau RDV PEB enregistre</h1>
    </div>
    <div style="background:#fff;padding:24px 28px;border:1px solid #e5e7eb;border-radius:0 0 12px 12px;">
        <p style="color:#374151;"><strong>{req.prenom} {req.nom}</strong> vient de prendre un RDV.</p>
        <table style="width:100%;border-collapse:collapse;margin:16px 0;">
            <tr><td style="padding:8px;color:#8a9bb5;font-size:0.82rem;font-weight:700;text-transform:uppercase;">Type</td><td style="padding:8px;color:#1B3A8C;font-weight:700;">{req.type_bien} - {req.superficie}</td></tr>
            <tr style="background:#f4f6fb;"><td style="padding:8px;color:#8a9bb5;font-size:0.82rem;font-weight:700;text-transform:uppercase;">Adresse</td><td style="padding:8px;color:#1B3A8C;font-weight:700;">{adresse}</td></tr>
            <tr><td style="padding:8px;color:#8a9bb5;font-size:0.82rem;font-weight:700;text-transform:uppercase;">Creneau</td><td style="padding:8px;color:#1B3A8C;font-weight:700;">{req.creneau_label}</td></tr>
            <tr style="background:#f4f6fb;"><td style="padding:8px;color:#8a9bb5;font-size:0.82rem;font-weight:700;text-transform:uppercase;">Tel</td><td style="padding:8px;color:#1B3A8C;font-weight:700;">{req.tel}</td></tr>
            <tr><td style="padding:8px;color:#8a9bb5;font-size:0.82rem;font-weight:700;text-transform:uppercase;">Email</td><td style="padding:8px;color:#1B3A8C;font-weight:700;">{req.email}</td></tr>
        </table>
        <div style="text-align:center;margin:20px 0;">
            <a href="https://www.pebepc.com/peb-dashboard" style="background:#1B3A8C;color:#fff;padding:12px 28px;border-radius:999px;text-decoration:none;font-weight:bold;">Voir le dashboard</a>
        </div>
    </div>
</div>"""
        send_odoo_mail(uid, models, ODOO_USER, subject_expert, body_expert)

        # ── Mail confirmation client ──
        subject_client = "Confirmation de votre demande de RDV PEB"
        body_client = f"""<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
    <div style="background:linear-gradient(135deg,#1B3A8C,#3B82F6);padding:24px 28px;border-radius:12px 12px 0 0;">
        <h1 style="color:#fff;font-size:1.2rem;margin:0;">Votre demande de RDV a bien ete enregistree</h1>
    </div>
    <div style="background:#fff;padding:24px 28px;border:1px solid #e5e7eb;border-radius:0 0 12px 12px;">
        <p style="color:#374151;">Bonjour <strong>{req.prenom} {req.nom}</strong>,</p>
        <p style="color:#374151;">Nous avons bien recu votre demande de RDV pour la certification PEB du bien situe au :</p>
        <p style="background:#f4f6fb;padding:12px;border-radius:8px;color:#1B3A8C;font-weight:bold;">{adresse}</p>
        <table style="width:100%;border-collapse:collapse;margin:16px 0;">
            <tr><td style="padding:8px;color:#8a9bb5;font-size:0.82rem;font-weight:700;text-transform:uppercase;">Type de bien</td><td style="padding:8px;color:#1B3A8C;font-weight:700;">{req.type_bien} - {req.superficie}</td></tr>
            <tr style="background:#f4f6fb;"><td style="padding:8px;color:#8a9bb5;font-size:0.82rem;font-weight:700;text-transform:uppercase;">Creneau souhaite</td><td style="padding:8px;color:#1B3A8C;font-weight:700;">{req.creneau_label}</td></tr>
        </table>
        <p style="color:#374151;">Notre expert vous contactera prochainement pour confirmer le rendez-vous.</p>
        <hr style="border:none;border-top:1px solid #e5e7eb;margin:20px 0;"/>
        <p style="color:#8a9bb5;font-size:0.78rem;">Armine Sotodeh - Expert PEB<br/>
        <a href="mailto:{ODOO_USER}" style="color:#1B3A8C;">{ODOO_USER}</a></p>
    </div>
</div>"""
        send_odoo_mail(uid, models, req.email, subject_client, body_client)

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
        pdf_b64   = base64.b64encode(pdf_bytes).decode("utf-8")

        models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "calendar.event", "write", [[event_id], {
            "x_studio_client_token":   token,
            "x_studio_statut_draft":   "draft_sent",
            "x_studio_pdf_provisoire": pdf_b64
        }])
        logging.info("PDF provisoire + token ecrits dans Odoo")

        infos        = get_mission_info(uid, models, event_id)
        client_email = infos.get("email", "")
        client_nom   = infos.get("nom", "le mandataire")
        adresse      = infos.get("adresse", "")
        client_link  = f"https://www.pebepc.com/peb-pulse-token?token={token}"

        if client_email:
            subject   = "Votre PEB provisoire est disponible"
            body_html = f"""<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
    <div style="background:linear-gradient(135deg,#1B3A8C,#3B82F6);padding:28px 32px;border-radius:12px 12px 0 0;">
        <h1 style="color:#fff;font-size:1.4rem;margin:0;">Votre PEB provisoire est pret</h1>
    </div>
    <div style="background:#fff;padding:28px 32px;border:1px solid #e5e7eb;border-radius:0 0 12px 12px;">
        <p style="color:#374151;">Bonjour <strong>{client_nom}</strong>,</p>
        <p style="color:#374151;">Votre certificat PEB provisoire pour le bien situe au :</p>
        <p style="background:#f4f6fb;padding:12px;border-radius:8px;color:#1B3A8C;font-weight:bold;">{adresse}</p>
        <p style="color:#374151;">est maintenant disponible.</p>
        <div style="text-align:center;margin:28px 0;">
    <a href="{client_link}" style="background:#1B3A8C;color:#fff;padding:14px 20px;border-radius:12px;text-decoration:none;font-weight:bold;font-size:0.95rem;display:inline-block;max-width:90%;word-break:break-word;">Consulter mon PEB provisoire</a>
</div>
        <p style="color:#8a9bb5;font-size:0.82rem;">Lien : <a href="{client_link}" style="color:#1B3A8C;">{client_link}</a></p>
        <hr style="border:none;border-top:1px solid #e5e7eb;margin:20px 0;"/>
        <p style="color:#8a9bb5;font-size:0.78rem;">Armine Sotodeh - Expert PEB</p>
    </div>
</div>"""
            send_odoo_mail(uid, models, client_email, subject, body_html)

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

        models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "calendar.event", "write", [[event_id], {"x_studio_pdf_definitif": pdf_b64}])
        models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "calendar.event", "write", [[event_id], {"x_studio_statut_draft": "closed"}])
        logging.info("PDF definitif + statut closed ecrits dans Odoo")

        infos        = get_mission_info(uid, models, event_id)
        client_email = infos.get("email", "")
        client_nom   = infos.get("nom", "le mandataire")
        adresse      = infos.get("adresse", "")

        token_ev = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "calendar.event", "read", [[event_id]], {"fields": ["x_studio_client_token"]})
        token    = token_ev[0].get("x_studio_client_token", "") if token_ev else ""
        pdf_link = f"https://www.pebepc.com/peb-pulse-token?token={token}" if token else ""

        if client_email:
            subject   = "Votre certificat PEB definitif est disponible"
            body_html = f"""<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
    <div style="background:linear-gradient(135deg,#059669,#10B981);padding:28px 32px;border-radius:12px 12px 0 0;">
        <h1 style="color:#fff;font-size:1.4rem;margin:0;">Votre certificat PEB definitif est pret</h1>
    </div>
    <div style="background:#fff;padding:28px 32px;border:1px solid #e5e7eb;border-radius:0 0 12px 12px;">
        <p style="color:#374151;">Bonjour <strong>{client_nom}</strong>,</p>
        <p style="color:#374151;">Votre certificat PEB <strong>definitif</strong> pour le bien situe au :</p>
        <p style="background:#f0fdf4;padding:12px;border-radius:8px;color:#16a34a;font-weight:bold;">{adresse}</p>
        {"<div style='text-align:center;margin:28px 0;'><a href='" + pdf_link + "' style='background:#10B981;color:#fff;padding:14px 20px;border-radius:12px;text-decoration:none;font-weight:bold;font-size:0.95rem;display:inline-block;max-width:90%;word-break:break-word;'>Telecharger mon certificat PEB</a></div>" if pdf_link else ""}
        <hr style="border:none;border-top:1px solid #e5e7eb;margin:20px 0;"/>
        <p style="color:#8a9bb5;font-size:0.78rem;">Armine Sotodeh - Expert PEB</p>
    </div>
</div>"""
            send_odoo_mail(uid, models, client_email, subject, body_html)

        return {"success": True, "mail_sent": bool(client_email)}

    except Exception as e:
        logging.error(f"send_final error: {e}")
        return {"success": False, "error": str(e)}


# ── SERVIR LES PDFs ────────────────────────────────────────

@app.get("/pebepc/mission/{token}/pdf")
def get_pdf_provisoire(token: str):
    try:
        uid, models = odoo_connect()
        events = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "calendar.event", "search_read",
            [[["x_studio_client_token", "=", token], ["active", "=", True]]],
            {"fields": ["id", "name", "x_studio_pdf_provisoire"], "limit": 1})
        if not events:
            return Response(content=b"Mission introuvable", status_code=404)
        ev      = events[0]
        pdf_b64 = ev.get("x_studio_pdf_provisoire")
        if not pdf_b64:
            return Response(content=b"Aucun PDF provisoire disponible", status_code=404)
        pdf_bytes = base64.b64decode(pdf_b64)
        return Response(content=pdf_bytes, media_type="application/pdf",
            headers={"Content-Disposition": f"inline; filename=\"PEB_provisoire_{ev['id']}.pdf\"", "Access-Control-Allow-Origin": "*"})
    except Exception as e:
        logging.error(f"get_pdf_provisoire error: {e}")
        return Response(content=str(e).encode(), status_code=500)


@app.get("/pebepc/mission/{token}/pdf/final")
def get_pdf_definitif(token: str):
    try:
        uid, models = odoo_connect()
        events = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "calendar.event", "search_read",
            [[["x_studio_client_token", "=", token], ["active", "=", True]]],
            {"fields": ["id", "name", "x_studio_pdf_definitif"], "limit": 1})
        if not events:
            return Response(content=b"Mission introuvable", status_code=404)
        ev      = events[0]
        pdf_b64 = ev.get("x_studio_pdf_definitif")
        if not pdf_b64:
            return Response(content="Aucun PDF definitif disponible".encode(), status_code=404)
        pdf_bytes = base64.b64decode(pdf_b64)
        return Response(content=pdf_bytes, media_type="application/pdf",
            headers={"Content-Disposition": f"inline; filename=\"PEB_definitif_{ev['id']}.pdf\"", "Access-Control-Allow-Origin": "*"})
    except Exception as e:
        logging.error(f"get_pdf_definitif error: {e}")
        return Response(content=str(e).encode(), status_code=500)


# ── MISSION CLIENT ─────────────────────────────────────────

@app.get("/pebepc/mission/{token}")
def get_mission(token: str):
    try:
        uid, models = odoo_connect()
        events = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "calendar.event", "search_read",
            [[["x_studio_client_token", "=", token], ["active", "=", True]]],
            {"fields": ["id", "name", "start", "x_studio_adresse_du_bien",
                        "x_studio_informations_sur_le_bien", "x_studio_statut_draft",
                        "x_studio_remarques_client", "x_studio_pdf_provisoire"], "limit": 1})
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
        events = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "calendar.event", "search_read",
            [[["x_studio_client_token", "=", token], ["active", "=", True]]],
            {"fields": ["id"], "limit": 1})
        if not events:
            return {"success": False, "error": "Mission introuvable"}
        event_id = events[0]["id"]
        models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "calendar.event", "write",
            [[event_id], {"x_studio_statut_draft": "draft_accepted"}])

        infos      = get_mission_info(uid, models, event_id)
        adresse    = infos.get("adresse", "")
        client_nom = infos.get("nom", "Le client")
        subject    = "PEB provisoire accepte par le client"
        body_html  = f"""<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
    <div style="background:linear-gradient(135deg,#059669,#10B981);padding:24px 28px;border-radius:12px 12px 0 0;">
        <h1 style="color:#fff;font-size:1.2rem;margin:0;">Le client a accepte le PEB provisoire</h1>
    </div>
    <div style="background:#fff;padding:24px 28px;border:1px solid #e5e7eb;border-radius:0 0 12px 12px;">
        <p style="color:#374151;"><strong>{client_nom}</strong> a accepte le PEB provisoire pour :</p>
        <p style="background:#f0fdf4;padding:12px;border-radius:8px;color:#16a34a;font-weight:bold;">{adresse}</p>
        <p style="color:#374151;">Vous pouvez maintenant envoyer le PEB definitif depuis le dashboard.</p>
        <div style="text-align:center;margin:20px 0;">
            <a href="https://www.pebepc.com/peb-dashboard" style="background:#1B3A8C;color:#fff;padding:12px 28px;border-radius:999px;text-decoration:none;font-weight:bold;">Voir le dashboard</a>
        </div>
    </div>
</div>"""
        send_odoo_mail(uid, models, ODOO_USER, subject, body_html)
        logging.info(f"Mission {event_id} acceptee")
        return {"success": True}
    except Exception as e:
        logging.error(f"accept_mission error: {e}")
        return {"success": False, "error": str(e)}


@app.post("/pebepc/mission/{token}/refuse")
def refuse_mission(token: str, req: RefuseRequest):
    try:
        uid, models = odoo_connect()
        events = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "calendar.event", "search_read",
            [[["x_studio_client_token", "=", token], ["active", "=", True]]],
            {"fields": ["id"], "limit": 1})
        if not events:
            return {"success": False, "error": "Mission introuvable"}
        event_id = events[0]["id"]
        models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "calendar.event", "write",
            [[event_id], {
                "x_studio_statut_draft":     "draft_refused",
                "x_studio_remarques_client": req.remarques or ""
            }])

        infos      = get_mission_info(uid, models, event_id)
        adresse    = infos.get("adresse", "")
        client_nom = infos.get("nom", "Le client")
        remarques  = req.remarques or "Aucune remarque fournie."
        subject    = "PEB provisoire refuse par le client"
        body_html  = f"""<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
    <div style="background:linear-gradient(135deg,#DC2626,#EF4444);padding:24px 28px;border-radius:12px 12px 0 0;">
        <h1 style="color:#fff;font-size:1.2rem;margin:0;">Le client a refuse le PEB provisoire</h1>
    </div>
    <div style="background:#fff;padding:24px 28px;border:1px solid #e5e7eb;border-radius:0 0 12px 12px;">
        <p style="color:#374151;"><strong>{client_nom}</strong> a refuse le PEB provisoire pour :</p>
        <p style="background:#fef2f2;padding:12px;border-radius:8px;color:#dc2626;font-weight:bold;">{adresse}</p>
        <p style="color:#374151;font-weight:700;">Remarques du client :</p>
        <p style="background:#fff8e7;padding:12px;border-radius:8px;color:#92610a;border:1px solid #f9ca66;">{remarques}</p>
        <p style="color:#374151;">Apportez les corrections et renvoyez un nouveau PEB provisoire.</p>
        <div style="text-align:center;margin:20px 0;">
            <a href="https://www.pebepc.com/peb-dashboard" style="background:#1B3A8C;color:#fff;padding:12px 28px;border-radius:999px;text-decoration:none;font-weight:bold;">Voir le dashboard</a>
        </div>
    </div>
</div>"""
        send_odoo_mail(uid, models, ODOO_USER, subject, body_html)
        logging.info(f"Mission {event_id} refusee")
        return {"success": True}
    except Exception as e:
        logging.error(f"refuse_mission error: {e}")
        return {"success": False, "error": str(e)}


# ── CHAT CLIENT ────────────────────────────────────────────

CHARLOTTE_PARTNER_ID = 12  # res.partner id de charlotte@pebepc.com


@app.get("/pebepc/chat/{token}")
def get_chat(token: str):
    try:
        uid, models = odoo_connect()
        events = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "calendar.event", "search_read",
            [[["x_studio_client_token", "=", token], ["active", "=", True]]],
            {"fields": ["id"], "limit": 1})
        if not events:
            return {"success": False, "error": "Mission introuvable"}
        event_id = events[0]["id"]

        msgs = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "mail.message", "search_read",
            [[["model", "=", "calendar.event"], ["res_id", "=", event_id],
              ["message_type", "in", ["comment", "email"]]]],
            {"fields": ["id", "author_id", "body", "date"], "order": "date asc", "limit": 100})

        result = []
        for msg in (msgs or []):
            aid = msg.get("author_id")
            result.append({
                "id":        msg["id"],
                "author":    aid[1] if aid else "Inconnu",
                "is_expert": bool(aid and aid[0] == CHARLOTTE_PARTNER_ID),
                "body":      msg.get("body", ""),
                "date":      msg.get("date", "")
            })
        return {"success": True, "messages": result}
    except Exception as e:
        logging.error(f"get_chat error: {e}")
        return {"success": False, "error": str(e)}


@app.post("/pebepc/chat/{token}")
def post_chat(token: str, req: ChatRequest):
    try:
        uid, models = odoo_connect()
        events = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "calendar.event", "search_read",
            [[["x_studio_client_token", "=", token], ["active", "=", True]]],
            {"fields": ["id", "partner_ids"], "limit": 1})
        if not events:
            return {"success": False, "error": "Mission introuvable"}
        event_id    = events[0]["id"]
        partner_ids = events[0].get("partner_ids", [])

        # Partenaire client = premier partenaire qui n'est pas Charlotte
        client_partner_id = next((p for p in partner_ids if p != CHARLOTTE_PARTNER_ID), None)

        kwargs = {"body": req.message, "message_type": "comment", "subtype_xmlid": "mail.mt_comment"}
        if client_partner_id:
            kwargs["author_id"] = client_partner_id

        models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "calendar.event", "message_post",
            [[event_id]], kwargs)
        return {"success": True}
    except Exception as e:
        logging.error(f"post_chat error: {e}")
        return {"success": False, "error": str(e)}


# ── DASHBOARD CLIENT ───────────────────────────────────────

@app.get("/pebepc/client/missions")
def get_client_missions(email: str):
    try:
        if not email or "@" not in email:
            return {"success": False, "error": "Email invalide"}
        uid, models = odoo_connect()

        # Find partner by email
        partners = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "search_read",
            [[["email", "=ilike", email.strip()]]],
            {"fields": ["id", "name"], "limit": 5})
        if not partners:
            return {"success": True, "client_name": "", "missions": []}

        partner_ids = [p["id"] for p in partners]
        client_name = partners[0]["name"]

        # Find all events where this partner is attendee
        events = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "calendar.event", "search_read",
            [[["partner_ids", "in", partner_ids], ["active", "=", True]]],
            {"fields": ["id", "name", "start", "x_studio_adresse_du_bien",
                        "x_studio_informations_sur_le_bien", "x_studio_statut_draft",
                        "x_studio_client_token", "x_studio_pdf_provisoire",
                        "x_studio_pdf_definitif"],
             "order": "start desc", "limit": 50})

        missions = []
        for ev in (events or []):
            infos     = ev.get("x_studio_informations_sur_le_bien", "") or ""
            type_bien = superficie = ""
            tm = re.search(r"Type\s*:\s*(.+)", infos)
            if tm: type_bien = tm.group(1).strip()
            sm = re.search(r"Superficie\s*:\s*(.+)", infos)
            if sm: superficie = sm.group(1).strip()

            statut_raw = str(ev.get("x_studio_statut_draft") or "false")
            statut_map = {
                "false":          {"label": "En attente",        "color": "#3B82F6"},
                "none":           {"label": "En attente",        "color": "#3B82F6"},
                "draft_sent":     {"label": "PEB provisoire",    "color": "#F59E0B"},
                "draft_refused":  {"label": "PEB refusé",        "color": "#EF4444"},
                "draft_accepted": {"label": "PEB accepté",       "color": "#10B981"},
                "closed":         {"label": "Dossier clôturé",   "color": "#6B7280"},
            }
            statut_info = statut_map.get(statut_raw, {"label": statut_raw, "color": "#6B7280"})

            token = ev.get("x_studio_client_token") or ""
            missions.append({
                "id":         ev["id"],
                "name":       ev["name"],
                "start":      ev.get("start", ""),
                "adresse":    ev.get("x_studio_adresse_du_bien", "") or "",
                "type_bien":  type_bien,
                "superficie": superficie,
                "statut":     statut_raw,
                "statut_label": statut_info["label"],
                "statut_color": statut_info["color"],
                "token":      token,
                "has_pdf":    bool(ev.get("x_studio_pdf_provisoire")),
                "has_pdf_final": bool(ev.get("x_studio_pdf_definitif")),
            })

        return {"success": True, "client_name": client_name, "missions": missions}
    except Exception as e:
        logging.error(f"get_client_missions error: {e}")
        return {"success": False, "error": str(e)}


# ── MCP (monté en dernier pour ne pas intercepter les routes ci-dessus) ──

app.mount("/", mcp_app)
