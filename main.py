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


# ── PAGE CLIENT ────────────────────────────────────────────

@app.get("/rdv-client", response_class=HTMLResponse)
def rdv_client(token: str = ""):
    if not token:
        return HTMLResponse("<h2>Lien invalide.</h2>", status_code=400)
    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Votre PEB provisoire</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter+Tight:wght@400;600;700;800;900&display=swap');
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Inter Tight',sans-serif;background:#f4f6fb;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}}
.card{{background:#fff;border-radius:20px;box-shadow:0 8px 40px rgba(27,58,140,0.13);width:100%;max-width:640px;overflow:hidden}}
.card-head{{background:linear-gradient(135deg,#1B3A8C,#3B82F6);padding:28px 32px;color:#fff}}
.card-head h1{{font-size:1.4rem;font-weight:900;margin-bottom:4px}}
.card-head p{{font-size:0.82rem;opacity:0.75;font-weight:600}}
.card-body{{padding:28px 32px;display:flex;flex-direction:column;gap:20px}}
.info-grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
.info-item label{{font-size:0.62rem;font-weight:800;text-transform:uppercase;letter-spacing:0.1em;color:#8a9bb5;display:block;margin-bottom:3px}}
.info-item span{{font-size:0.9rem;font-weight:700;color:#1B3A8C}}
.pdf-frame{{width:100%;height:420px;border:1.5px solid rgba(27,58,140,0.13);border-radius:12px;background:#f8faff}}
.pdf-fallback{{display:none;padding:20px;text-align:center;background:#f8faff;border-radius:12px;border:1.5px solid rgba(27,58,140,0.13)}}
.pdf-fallback a{{color:#1B3A8C;font-weight:700;font-size:0.88rem}}
.actions{{display:flex;gap:12px}}
.btn{{flex:1;padding:14px;border-radius:12px;border:none;font-size:0.9rem;font-weight:800;cursor:pointer;font-family:'Inter Tight',sans-serif;transition:all 0.18s}}
.btn-accept{{background:#10B981;color:#fff}}
.btn-accept:hover{{background:#059669}}
.btn-refuse{{background:#fff;color:#EF4444;border:2px solid #EF4444}}
.btn-refuse:hover{{background:#EF4444;color:#fff}}
.refuse-box{{display:none;flex-direction:column;gap:10px}}
.refuse-box textarea{{width:100%;padding:12px;border-radius:10px;border:1.5px solid rgba(27,58,140,0.2);font-family:'Inter Tight',sans-serif;font-size:0.84rem;resize:vertical;min-height:90px;outline:none}}
.refuse-box textarea:focus{{border-color:#1B3A8C}}
.btn-confirm-refuse{{background:#EF4444;color:#fff;padding:12px;border-radius:10px;border:none;font-size:0.88rem;font-weight:800;cursor:pointer;font-family:'Inter Tight',sans-serif}}
.btn-confirm-refuse:hover{{background:#DC2626}}
.result{{display:none;padding:20px;border-radius:12px;text-align:center;font-weight:800;font-size:1rem}}
.result.success{{background:#f0fdf4;color:#16a34a;border:1.5px solid #bbf7d0}}
.result.error{{background:#fef2f2;color:#dc2626;border:1.5px solid #fecaca}}
.loader{{text-align:center;padding:40px;color:#8a9bb5;font-weight:700}}
@media(max-width:500px){{
  .card-head,.card-body{{padding:20px}}
  .info-grid{{grid-template-columns:1fr}}
  .pdf-frame{{height:280px}}
}}
</style>
</head>
<body>
<div class="card">
  <div class="card-head">
    <h1>📄 Votre PEB provisoire</h1>
    <p>Veuillez consulter le document ci-dessous puis accepter ou refuser.</p>
  </div>
  <div class="card-body" id="main-body">
    <div class="loader" id="loader">Chargement de votre mission…</div>
  </div>
</div>

<script>
var TOKEN = "{token}";
var API   = "{RAILWAY_URL}";

fetch(API + '/pebepc/mission/' + TOKEN)
  .then(function(r){{ return r.json(); }})
  .then(function(data){{
    var body = document.getElementById('main-body');
    document.getElementById('loader').remove();

    if (!data.success) {{
      body.innerHTML = '<div class="result error" style="display:block">❌ Mission introuvable ou lien invalide.</div>';
      return;
    }}

    var m = data.mission;

    if (m.statut === 'draft_accepted') {{
      body.innerHTML = '<div class="result success" style="display:block">✓ Vous avez déjà accepté ce PEB provisoire. Merci !</div>';
      return;
    }}
    if (m.statut === 'draft_refused') {{
      body.innerHTML = '<div class="result error" style="display:block">Vous avez déjà refusé ce PEB provisoire. L\'expert a été notifié.</div>';
      return;
    }}

    var infoGrid = '<div class="info-grid">'
      + '<div class="info-item"><label>Adresse du bien</label><span>' + (m.adresse||'—') + '</span></div>'
      + '<div class="info-item"><label>Type de bien</label><span>' + (m.type_bien||'—') + '</span></div>'
      + '<div class="info-item"><label>Superficie</label><span>' + (m.superficie||'—') + '</span></div>'
      + '<div class="info-item"><label>Mandataire</label><span>' + (m.client_nom||'—') + '</span></div>'
      + '</div>';

    var pdfUrl = API + '/pebepc/mission/' + TOKEN + '/pdf';

    body.innerHTML = infoGrid
      + '<iframe class="pdf-frame" id="pdf-frame" src="' + pdfUrl + '" title="PEB provisoire"></iframe>'
      + '<div class="pdf-fallback" id="pdf-fallback"><p style="margin-bottom:8px;color:#8a9bb5;font-size:0.82rem;">Si le PDF ne s\'affiche pas :</p><a href="' + pdfUrl + '" target="_blank">📥 Télécharger le PDF</a></div>'
      + '<div class="actions" id="actions">'
      + '<button class="btn btn-refuse" onclick="showRefuse()">✗ Refuser</button>'
      + '<button class="btn btn-accept" onclick="doAccept()">✓ Accepter</button>'
      + '</div>'
      + '<div class="refuse-box" id="refuse-box">'
      + '<label style="font-size:0.72rem;font-weight:800;color:#8a9bb5;text-transform:uppercase;letter-spacing:0.08em;">Motif du refus (optionnel)</label>'
      + '<textarea id="remarques" placeholder="Expliquez pourquoi vous refusez ce PEB provisoire…"></textarea>'
      + '<button class="btn-confirm-refuse" onclick="doRefuse()">Confirmer le refus</button>'
      + '</div>'
      + '<div class="result" id="result"></div>';

    document.getElementById('pdf-frame').addEventListener('error', function(){{
      document.getElementById('pdf-fallback').style.display = 'block';
    }});
  }})
  .catch(function(){{
    document.getElementById('loader').innerHTML = '❌ Erreur de chargement. Vérifiez votre connexion.';
  }});

function showRefuse(){{
  document.getElementById('actions').style.display   = 'none';
  document.getElementById('refuse-box').style.display = 'flex';
}}

function doAccept(){{
  document.getElementById('actions').style.display = 'none';
  fetch(API + '/pebepc/mission/' + TOKEN + '/accept', {{method:'POST'}})
    .then(function(r){{ return r.json(); }})
    .then(function(d){{
      var el = document.getElementById('result');
      el.style.display = 'block';
      if (d.success){{
        el.className = 'result success';
        el.innerHTML = '✓ Merci ! Vous avez accepté le PEB provisoire. L\'expert va maintenant préparer la version définitive.';
      }} else {{
        el.className = 'result error';
        el.innerHTML = '❌ Erreur : ' + (d.error||'');
        document.getElementById('actions').style.display = 'flex';
      }}
    }});
}}

function doRefuse(){{
  var remarques = document.getElementById('remarques').value.trim();
  document.getElementById('refuse-box').style.display = 'none';
  fetch(API + '/pebepc/mission/' + TOKEN + '/refuse', {{
    method: 'POST',
    headers: {{'Content-Type':'application/json'}},
    body: JSON.stringify({{remarques: remarques}})
  }})
    .then(function(r){{ return r.json(); }})
    .then(function(d){{
      var el = document.getElementById('result');
      el.style.display = 'block';
      if (d.success){{
        el.className = 'result error';
        el.innerHTML = '↩ Refus enregistré. L\'expert a été notifié et va corriger le PEB provisoire.';
      }} else {{
        el.className = 'result error';
        el.innerHTML = '❌ Erreur : ' + (d.error||'');
        document.getElementById('refuse-box').style.display = 'flex';
      }}
    }});
}}
</script>
</body>
</html>"""
    return HTMLResponse(html)


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

        attachments = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "ir.attachment", "search_read",
            [[
                ["res_model", "=", "calendar.event"],
                ["res_id", "=", event_id],
                ["mimetype", "=", "application/pdf"]
            ]],
            {"fields": ["id", "name", "datas"], "limit": 1, "order": "id desc"}
        )
        if not attachments:
            return Response(content=b"PDF introuvable", status_code=404)

        pdf_bytes = base64.b64decode(attachments[0]["datas"])
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
