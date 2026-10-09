"""Temps de trajet entre l'adresse d'un client et les rendez-vous déjà prévus.

Utilise OpenRouteService (https://openrouteservice.org) :
  - géocodage : adresse -> coordonnées GPS (limité à la Belgique) ;
  - matrice   : temps de trajet en voiture d'un point vers plusieurs points, en un seul appel.

La clé API se règle dans la variable d'environnement ORS_API_KEY (Railway > Variables).
Sans clé, ou si le service ne répond pas, les fonctions renvoient None et le site
garde le comportement habituel (marge fixe de 30 min entre deux rendez-vous).
"""
import os
import logging
import math
from collections import OrderedDict

import requests

ORS_API_KEY = os.getenv("ORS_API_KEY", "")
ORS_BASE = "https://api.openrouteservice.org"
TIMEOUT = 8  # secondes

# Cache des géocodages (adresse normalisée -> (lon, lat) ou None), conservé tant que le serveur tourne.
_GEOCODE_CACHE: "OrderedDict[str, object]" = OrderedDict()
_GEOCODE_CACHE_MAX = 2000


def enabled() -> bool:
    return bool(ORS_API_KEY)


def _norm(address: str) -> str:
    return " ".join((address or "").lower().replace(",", " ").split())


def geocode(address: str):
    """Renvoie (lon, lat) pour une adresse en Belgique, ou None si introuvable."""
    key = _norm(address)
    if not key or not enabled():
        return None
    if key in _GEOCODE_CACHE:
        _GEOCODE_CACHE.move_to_end(key)
        return _GEOCODE_CACHE[key]
    coords = None
    try:
        r = requests.get(f"{ORS_BASE}/geocode/search", params={
            "api_key": ORS_API_KEY, "text": address, "boundary.country": "BE", "size": 1,
        }, timeout=TIMEOUT)
        r.raise_for_status()
        feats = r.json().get("features") or []
        if feats:
            lon, lat = feats[0]["geometry"]["coordinates"][:2]
            coords = (float(lon), float(lat))
    except Exception as e:
        logging.warning(f"ORS geocode '{address}': {e}")
        return None  # erreur réseau : on ne met pas en cache, on réessaiera
    _GEOCODE_CACHE[key] = coords
    if len(_GEOCODE_CACHE) > _GEOCODE_CACHE_MAX:
        _GEOCODE_CACHE.popitem(last=False)
    return coords


def durations_from(origin, destinations):
    """Temps de trajet en voiture (minutes, arrondis au-dessus) de origin vers chaque destination.

    Un seul appel « matrice ». Les trajets aller et retour sont considérés comme équivalents.
    Renvoie une liste (None pour une destination injoignable) ou None en cas d'erreur.
    """
    if not enabled() or origin is None or not destinations:
        return [] if destinations == [] else None
    out = []
    # 3500 éléments max par requête ORS : on découpe largement en dessous
    for i in range(0, len(destinations), 500):
        chunk = destinations[i:i + 500]
        try:
            r = requests.post(f"{ORS_BASE}/v2/matrix/driving-car", json={
                "locations": [list(origin)] + [list(d) for d in chunk],
                "sources": [0],
                "destinations": list(range(1, len(chunk) + 1)),
                "metrics": ["duration"],
            }, headers={"Authorization": ORS_API_KEY}, timeout=TIMEOUT)
            r.raise_for_status()
            row = (r.json().get("durations") or [[]])[0]
            out.extend(None if v is None else int(math.ceil(v / 60.0)) for v in row)
        except Exception as e:
            logging.warning(f"ORS matrix: {e}")
            return None
    return out


def event_address(ev: dict) -> str:
    """Adresse d'un calendar.event : champ Studio, sinon lieu, sinon fin du nom (« … — Rue …, 1000 Ville »)."""
    addr = (ev.get("x_studio_adresse_du_bien") or "").strip()
    if not addr:
        addr = (ev.get("location") or "").strip()
    if not addr and ev.get("name") and "— " in ev["name"]:
        addr = ev["name"].split("— ")[-1].strip()
    return addr
