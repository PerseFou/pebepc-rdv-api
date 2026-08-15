import os
import re
import xmlrpc.client
import logging

ODOO_URL      = os.getenv("ODOO_URL", "https://peb-pulls.odoo.com")
ODOO_DB       = os.getenv("ODOO_DB", "peb-pulls")
ODOO_USER     = os.getenv("ODOO_USER", "armine.sotodeh10@gmail.com")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD", "")
EXPERT_NAME   = "Armine Sotodeh"


def odoo_connect():
    logging.info(f"Connexion Odoo: {ODOO_URL} / {ODOO_DB} / {ODOO_USER}")
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    logging.info(f"UID obtenu: {uid}")
    if not uid:
        raise Exception("Authentification Odoo echouee")
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
