"""Corrige les RDV dont l'experte est le compte technique (organisateur API).

Pour chaque RDV (hors congés INDISPO) dont user_id n'est pas une experte :
- si une experte (Charlotte ou Emma) est parmi les participants, elle devient user_id ;
- sinon le RDV est listé « à attribuer » (rien n'est modifié).

Usage (dans le dossier du dépôt, avec les mêmes variables que Railway) :
    set ODOO_PASSWORD=...            (PowerShell : $env:ODOO_PASSWORD="...")
    python fix_experts.py            -> simulation, n'écrit rien
    python fix_experts.py --apply    -> applique les corrections
"""
import sys
import types

# main.py importe le serveur MCP et le module trajets : inutiles ici
from starlette.applications import Starlette  # noqa: E402
sys.modules.setdefault("mcp_server", types.SimpleNamespace(
    mcp=types.SimpleNamespace(streamable_http_app=Starlette, session_manager=None)))

from odoo_client import odoo_connect, ODOO_DB, ODOO_PASSWORD  # noqa: E402


def main():
    apply = "--apply" in sys.argv
    uid, models = odoo_connect()

    import main as api  # réutilise la même liste d'expertes que l'API
    experts = api._load_experts(uid, models)
    if not experts:
        print("Aucune experte trouvée (Charlotte / Emma) : rien à faire.")
        return
    by_partner = {e["partner_id"]: u for u, e in experts.items() if e["partner_id"]}
    print("Expertes :", ", ".join(f"{e['name']} (user {u})" for u, e in experts.items()))

    events = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "calendar.event", "search_read",
        [["&", ["name", "not ilike", "INDISPO"], ["user_id", "not in", list(experts)],
          "|", ["active", "=", True], ["active", "=", False]]],
        {"fields": ["id", "name", "start", "user_id", "partner_ids"], "order": "start desc",
         "context": {"active_test": False}})

    fixed, todo = 0, []
    for ev in events or []:
        match = next((by_partner[p] for p in ev.get("partner_ids") or [] if p in by_partner), None)
        if match:
            print(f"  RDV {ev['id']} {ev['start']} {ev['name'][:60]!r} -> {experts[match]['name']}")
            if apply:
                models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "calendar.event", "write",
                    [[ev["id"]], {"user_id": match}])
            fixed += 1
        else:
            todo.append(ev)

    print(f"\n{fixed} RDV {'corrigés' if apply else 'à corriger (simulation)'}.")
    if todo:
        print(f"{len(todo)} RDV sans experte parmi les participants (à attribuer à la main) :")
        for ev in todo:
            print(f"  RDV {ev['id']} {ev['start']} {ev['name'][:70]!r}")
    if not apply and fixed:
        print("\nRelancez avec --apply pour appliquer.")


if __name__ == "__main__":
    main()
