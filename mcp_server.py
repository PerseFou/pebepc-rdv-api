from mcp.server.fastmcp import FastMCP
from odoo_client import odoo_connect, get_mission_info, ODOO_DB, ODOO_PASSWORD

mcp = FastMCP("pebepc-odoo", streamable_http_path="/")


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
