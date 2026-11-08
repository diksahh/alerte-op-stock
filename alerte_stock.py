#!/usr/bin/env python3
"""Alerte de remise en stock One Piece TCG (OP-01 à OP-18) -> Telegram.

Deux méthodes de surveillance :
  1. boutiques Shopify : lecture de /products.json (fiable, scanne tout le catalogue)
  2. pages produit classiques : détection d'un texte "rupture" / "dispo" dans la page
"""
import json
import os
import re
import time
from urllib.parse import urlparse

import requests
import yaml

FICHIER_CONFIG = "config.yaml"
FICHIER_ETAT = "etat.json"
UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

# "OP-16", "OP16", "OP 16", "OP-01", et les codes de cartes "OP16-118"
RE_SET = re.compile(r"\bOP\s?[-_]?\s?0?(\d{1,2})\b", re.I)

# Tout ce qu'on ne veut pas : japonais/chinois/coréen + accessoires + starters + cartes à l'unité
RE_EXCLU = re.compile(
    r"\b(jp|jap|japonais|japanese|japan|chinois|chinese|cor[ée]en|korean|"
    r"sleeves?|prot[èe]ge|playmat|tapis|classeur|binder|starter|single)\b", re.I)

# Ordre important : tin et display sont testés avant booster
TYPES = {
    "tin": re.compile(r"\b(tin|bo[iî]te\s+m[ée]tal)\b", re.I),
    "display": re.compile(r"\b(display|booster\s+box|box\s+of\s+24|bo[iî]te\s+de\s+24|"
                          r"24\s+(boosters|packs))\b", re.I),
    "booster": re.compile(r"\b(boosters?|packs?)\b", re.I),
}

# "FR"/"EN" en majuscules uniquement, pour ne pas confondre avec le mot français "en"
LANGUES = {
    "fr": re.compile(r"(?i:\b(vf|fran[cç]ais|french)\b)|\bFR\b"),
    "en": re.compile(r"(?i:\b(eng|anglais|english)\b)|\bEN\b"),
}

RUPTURE_DEFAUT = ["rupture de stock", "épuisé", "indisponible", "out of stock", "sold out"]
EMOJI = {"display": "📦", "booster": "🃏", "tin": "🥫"}


def analyser(titre, cfg):
    """Renvoie {set, type, langue} si le produit nous intéresse, sinon None."""
    if RE_EXCLU.search(titre):
        return None
    m = RE_SET.search(titre)
    if not m:
        return None
    num = int(m.group(1))
    if not cfg["sets"]["min"] <= num <= cfg["sets"]["max"]:
        return None
    type_ = next((t for t, rx in TYPES.items() if rx.search(titre)), None)
    if type_ not in cfg["types"]:
        return None
    langue = next((l for l, rx in LANGUES.items() if rx.search(titre)), None)
    if langue is None:
        if not cfg.get("langue_inconnue", True):
            return None
        langue = "?"
    elif langue not in cfg["langues"]:
        return None
    return {"set": f"OP-{num:02d}", "type": type_, "langue": langue}


def scanner_shopify(b, cfg):
    base = b["url"].rstrip("/")
    racine = "{0.scheme}://{0.netloc}".format(urlparse(base))
    for page in range(1, 41):
        r = requests.get(f"{base}/products.json", params={"limit": 250, "page": page},
                         headers=UA, timeout=30)
        r.raise_for_status()
        produits = r.json().get("products", [])
        if not produits:
            break
        for p in produits:
            info = analyser(p["title"], cfg)
            if not info:
                continue
            variantes = p.get("variants") or []
            dispo = any(v.get("available") for v in variantes)
            prix = next((v.get("price") for v in variantes if v.get("available")),
                        variantes[0].get("price") if variantes else None)
            lien = f"{racine}/products/{p['handle']}"
            yield {"cle": lien, "boutique": b["nom"], "titre": p["title"], "lien": lien,
                   "dispo": dispo, "prix": prix, "devise": b.get("devise", "€"), **info}
        if len(produits) < 250:
            break
        time.sleep(1)


def verifier_page(pg):
    r = requests.get(pg["url"], headers=UA, timeout=30)
    r.raise_for_status()
    texte = r.text.lower()
    if pg.get("texte_dispo"):
        dispo = any(t.lower() in texte for t in pg["texte_dispo"])
    else:
        dispo = not any(t.lower() in texte for t in pg.get("texte_rupture", RUPTURE_DEFAUT))
    return {"cle": pg["url"], "boutique": pg.get("boutique", urlparse(pg["url"]).netloc),
            "titre": pg["nom"], "lien": pg["url"], "dispo": dispo, "prix": None,
            "devise": "", "set": "", "type": pg.get("type", ""), "langue": ""}


def message(p):
    lignes = [f"{EMOJI.get(p['type'], '🔔')} EN STOCK — {p['boutique']}", p["titre"]]
    details = [p["set"], p["type"], p["langue"].upper(),
               f"{p['prix']} {p['devise']}".strip() if p["prix"] else ""]
    details = " · ".join(d for d in details if d)
    if details:
        lignes.append(details)
    lignes.append(p["lien"])
    return "\n".join(lignes)


def envoyer(texte):
    tok, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        print("[pas de Telegram configuré]\n" + texte)
        return
    r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      data={"chat_id": chat, "text": texte}, timeout=20)
    if not r.ok:
        print("Erreur Telegram :", r.text)


def main():
    with open(FICHIER_CONFIG, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    etat = {}
    if os.path.exists(FICHIER_ETAT):
        with open(FICHIER_ETAT, encoding="utf-8") as f:
            etat = json.load(f)
    premier_lancement = not etat

    vus, erreurs = {}, []
    for b in cfg.get("boutiques_shopify") or []:
        try:
            for prod in scanner_shopify(b, cfg):
                vus[prod["cle"]] = prod
        except Exception as e:
            erreurs.append(f"{b['nom']} : {e}")
    for pg in cfg.get("pages") or []:
        try:
            prod = verifier_page(pg)
            vus[prod["cle"]] = prod
        except Exception as e:
            erreurs.append(f"{pg['nom']} : {e}")

    alertes = []
    for cle, prod in vus.items():
        # alerte uniquement au passage "rupture -> en stock" (ou nouveau produit déjà dispo)
        if prod["dispo"] and not etat.get(cle, False) and not premier_lancement:
            alertes.append(prod)
        etat[cle] = prod["dispo"]

    if premier_lancement:
        n = sum(p["dispo"] for p in vus.values())
        envoyer(f"✅ Alerte stock One Piece activée\n{len(vus)} produits suivis, "
                f"{n} en stock actuellement.")
    for p in alertes:
        envoyer(message(p))

    with open(FICHIER_ETAT, "w", encoding="utf-8") as f:
        json.dump(etat, f, ensure_ascii=False, indent=1, sort_keys=True)

    print(f"{len(vus)} produits suivis, {len(alertes)} alerte(s) envoyée(s)")
    for e in erreurs:
        print("ERREUR", e)


if __name__ == "__main__":
    main()
