import re
from mcp.server.fastmcp import FastMCP
from odoo_client import odoo_connect, get_mission_info, ODOO_DB, ODOO_PASSWORD

mcp = FastMCP("pebepc-odoo")


def _make_key(url: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", url.strip("/").lower()).strip("_") or "page"
    return f"website.pebepc_{slug}"


def _wrap_qweb(html: str, key: str) -> str:
    return (
        f'<t t-name="{key}">\n'
        '  <t t-call="website.layout">\n'
        '    <div id="wrap" class="oe_structure">\n'
        f'      {html}\n'
        '    </div>\n'
        '  </t>\n'
        '</t>'
    )


@mcp.tool()
def search_missions(start: str = "", stop: str = "", statut: str = "", limit: int = 50) -> list[dict]:
    """Recherche des RDV/missions PEB dans Odoo (calendar.event).
    start/stop: bornes de dates ISO optionnelles (ex: '2026-08-01 00:00:00').
    statut: filtre optionnel sur x_studio_statut_draft (ex: draft_sent, draft_accepted, closed)."""
    uid, models = odoo_connect()
    domain = [["active", "=", True]]
    if start:
        domain.append(["start", ">=", start])
    if stop:
        domain.append(["stop", "<=", stop])
    if statut:
        domain.append(["x_studio_statut_draft", "=", statut])
    return models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD, "calendar.event", "search_read",
        [domain],
        {"fields": ["id", "name", "start", "stop", "x_studio_adresse_du_bien", "x_studio_statut_draft"], "limit": limit}
    )


@mcp.tool()
def get_mission_detail(event_id: int) -> dict:
    """Retourne le detail d'une mission PEB (adresse, mandataire, contact) a partir de son event_id Odoo."""
    uid, models = odoo_connect()
    return get_mission_info(uid, models, event_id)


@mcp.tool()
def search_clients(query: str, limit: int = 20) -> list[dict]:
    """Recherche des contacts Odoo (res.partner) par nom ou email."""
    uid, models = odoo_connect()
    domain = ["|", ["name", "ilike", query], ["email", "ilike", query]]
    return models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "search_read",
        [domain],
        {"fields": ["id", "name", "email", "phone"], "limit": limit}
    )


# ── WEBSITE BUILDER (pebepc.com) ─────────────────────────────

@mcp.tool()
def list_website_pages(limit: int = 100) -> list[dict]:
    """Liste les pages du site web Odoo (website.page) : id, nom, url, statut de publication."""
    uid, models = odoo_connect()
    return models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD, "website.page", "search_read",
        [[]],
        {"fields": ["id", "name", "url", "is_published", "view_id"], "limit": limit}
    )


@mcp.tool()
def get_page_html(page_id: int) -> dict:
    """Retourne le HTML/QWeb brut (arch_db) d'une page du site, a partir de son website.page id.
    Utile pour lire le contenu actuel avant de le modifier avec update_page_html."""
    uid, models = odoo_connect()
    pages = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD, "website.page", "read",
        [[page_id]],
        {"fields": ["id", "name", "url", "is_published", "view_id"]}
    )
    if not pages:
        return {"error": "page introuvable"}
    page = pages[0]
    view_id = page["view_id"][0] if page.get("view_id") else None
    if not view_id:
        return {"error": "pas de vue associee a cette page"}
    views = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD, "ir.ui.view", "read",
        [[view_id]],
        {"fields": ["id", "name", "arch_db", "key"]}
    )
    view = views[0] if views else {}
    return {
        "page_id": page["id"],
        "name": page["name"],
        "url": page["url"],
        "is_published": page["is_published"],
        "view_id": view_id,
        "view_key": view.get("key"),
        "html": view.get("arch_db")
    }


@mcp.tool()
def update_page_html(page_id: int, html: str) -> dict:
    """Remplace le contenu HTML/QWeb (arch_db) d'une page existante du site, identifiee par son
    website.page id. ATTENTION : le HTML doit rester une structure QWeb valide (meme squelette
    que ce que retourne get_page_html), sinon la page cassera en production des l'enregistrement.
    Toujours appeler get_page_html d'abord pour recuperer et adapter le contenu existant plutot
    que d'ecrire une structure QWeb inedite."""
    uid, models = odoo_connect()
    pages = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD, "website.page", "read",
        [[page_id]],
        {"fields": ["view_id"]}
    )
    if not pages or not pages[0].get("view_id"):
        return {"success": False, "error": "page ou vue introuvable"}
    view_id = pages[0]["view_id"][0]
    models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD, "ir.ui.view", "write",
        [[view_id], {"arch_db": html}]
    )
    return {"success": True, "page_id": page_id, "view_id": view_id}


@mcp.tool()
def create_website_page(name: str, url: str, is_published: bool = False) -> dict:
    """Cree une nouvelle page vide sur le site web Odoo. Utilise ensuite update_page_html
    pour remplir le contenu, ou prefere create_page_with_html pour tout faire en une etape."""
    uid, models = odoo_connect()
    result = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD, "website", "new_page",
        [],
        {"name": name, "url": url if url.startswith("/") else f"/{url}",
         "is_published": is_published, "add_menu": False}
    )
    return {"success": True, "result": result}


@mcp.tool()
def create_page_with_html(name: str, url: str, html: str, is_published: bool = False) -> dict:
    """Cree une page sur le site web Odoo avec du contenu HTML directement.
    Le HTML est automatiquement encapsule dans le squelette QWeb d'Odoo.
    name: titre affiché dans Odoo. url: chemin de la page (ex: /services ou services).
    html: contenu HTML de la page (balises <section>, <div>, <p>... — pas besoin de <html>/<body>).
    is_published: True pour rendre la page visible immediatement sur le site."""
    uid, models = odoo_connect()
    if not url.startswith("/"):
        url = f"/{url}"
    key = _make_key(url)
    arch = _wrap_qweb(html, key)
    view_id = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD, "ir.ui.view", "create",
        [{"name": name, "type": "qweb", "key": key, "arch_db": arch}]
    )
    page_id = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD, "website.page", "create",
        [{"name": name, "url": url, "view_id": view_id, "is_published": is_published}]
    )
    return {"success": True, "page_id": page_id, "view_id": view_id, "url": url, "key": key}


@mcp.tool()
def publish_page(page_id: int, published: bool = True) -> dict:
    """Publie ou depublie une page du site web Odoo.
    published=True rend la page visible sur le site, False la repasse en brouillon."""
    uid, models = odoo_connect()
    models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD, "website.page", "write",
        [[page_id], {"is_published": published}]
    )
    return {"success": True, "page_id": page_id, "is_published": published}


@mcp.tool()
def delete_website_page(page_id: int) -> dict:
    """Supprime une page du site web Odoo (website.page + vue associee).
    ATTENTION : action irreversible."""
    uid, models = odoo_connect()
    pages = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD, "website.page", "read",
        [[page_id]], {"fields": ["view_id"]}
    )
    if not pages:
        return {"success": False, "error": "page introuvable"}
    view_id = pages[0]["view_id"][0] if pages[0].get("view_id") else None
    models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "website.page", "unlink", [[page_id]])
    if view_id:
        try:
            models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "ir.ui.view", "unlink", [[view_id]])
        except Exception:
            pass
    return {"success": True, "page_id": page_id}
