#!/usr/bin/env python3
"""Alerte de remise en stock TCG -> Telegram (version 2).

Pour chaque boutique, le script trouve tout seul comment lire le catalogue :
  1. Shopify      -> /products.json (stock exact)
  2. WooCommerce  -> /wp-json/wc/store/v1/products (stock exact)
  3. Autres sites -> page de recherche ou de catégorie, puis lecture de chaque fiche produit
"""
import html as htmlmod
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote_plus, urljoin, urlparse

import requests
import yaml

FICHIER_CONFIG = "config.yaml"
FICHIER_ETAT = "etat.json"
MAX_FICHES = 60     # fiches produit lues au maximum par boutique et par passage
PAUSE = 0.6         # secondes entre deux requêtes sur une même boutique
TIMEOUT = 25
UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

# Pages de recherche essayées l'une après l'autre (PrestaShop, WooCommerce, Magento, etc.)
MODELES_RECHERCHE = [
    "/recherche?controller=search&s={q}",
    "/recherche?controller=search&search_query={q}",
    "/?s={q}&post_type=product",
    "/search?q={q}&type=product",
    "/catalogsearch/result/?q={q}",
    "/search?text={q}",
    "/recherche?q={q}",
    "/search?q={q}",
]

RE_EXCLU = re.compile(
    r"\b(jp|jpn|jap|japonais|japanese|japanisch|japan|chinois|chinese|chn|cn|"
    r"cor[ée]en|coreen|korean|kr|sleeves?|prot[èe]ge|playmat|tapis|classeur|binder|"
    r"starter|single|figurine|calendrier|portfolio|toploader)\b", re.I)

# Ordre important : tin et display sont testés avant booster
TYPES = {
    "tin": re.compile(r"\b(tin|bo[iî]te\s+m[ée]tal)\b", re.I),
    "display": re.compile(r"\b(display|booster\s+box|box\s+of\s+24|bo[iî]te\s+de\s+24|"
                          r"24\s+(boosters|packs))\b", re.I),
    "booster": re.compile(r"\b(boosters?|packs?)\b", re.I),
}
RE_FR = re.compile(r"\b(fr|vf|fran[cç]ais|french)\b", re.I)
RE_EN = re.compile(r"(?i:\b(eng|anglais|english|englisch)\b)|\bEN\b")  # "en" minuscule = mot français

DISPO_OUI = {"instock", "preorder", "presale", "limitedavailability", "onlineonly", "available"}
DISPO_NON = {"outofstock", "soldout", "discontinued", "oos", "backorder", "instoreonly",
             "unavailable"}
EMOJI = {"display": "📦", "booster": "🃏", "tin": "🥫"}


class PasCeType(Exception):
    """La boutique ne fonctionne pas avec cette méthode."""


# ---------------------------------------------------------------- analyse des titres
def slug_texte(url):
    """'.../display-op-15-one-piece-en.html' -> 'display op 15 one piece en EN'"""
    morceaux = [m for m in urlparse(url or "").path.split("/") if m]
    if not morceaux:
        return ""
    slug = re.sub(r"\.(html?|php)$", "", morceaux[-1].lower())
    mots = re.split(r"[-_+]+", slug)
    texte = " ".join(mots)
    if mots and mots[-1] in ("en", "eng"):
        texte += " EN"
    return texte


def analyser(texte, recherches):
    """Renvoie {jeu, set, type, langue} si le produit nous intéresse, sinon None."""
    if RE_EXCLU.search(texte):
        return None
    for r in recherches:
        m = r["_motif"].search(texte)
        if not m:
            continue
        num = int(m.group(1))
        if not r["min"] <= num <= r["max"]:
            continue
        type_ = next((t for t, rx in TYPES.items() if rx.search(texte)), None)
        if type_ not in r["types"]:
            continue
        langue = "fr" if RE_FR.search(texte) else "en" if RE_EN.search(texte) else "?"
        if langue == "?" and not r.get("langue_inconnue", True):
            continue
        if langue != "?" and langue not in r["langues"]:
            continue
        return {"jeu": r["nom"], "set": f"{r.get('prefixe', '')}-{num:02d}",
                "type": type_, "langue": langue}
    return None


def produit(b, lien, titre, dispo, prix, devise, info, precommande=False):
    return {"cle": lien, "boutique": b["nom"], "titre": titre, "lien": lien, "dispo": dispo,
            "prix": prix, "devise": devise, "precommande": precommande, **info}


def racine_de(url):
    return "{0.scheme}://{0.netloc}".format(urlparse(url))


def lire_json(r):
    try:
        return r.json()
    except ValueError:
        raise PasCeType("pas de JSON")


# ---------------------------------------------------------------- méthode 1 : Shopify
def via_shopify(s, b, recherches):
    base = b["url"].rstrip("/")
    out = []
    for page in range(1, 41):
        r = s.get(f"{base}/products.json", params={"limit": 250, "page": page}, timeout=TIMEOUT)
        if r.status_code != 200:
            if page == 1:
                raise PasCeType(f"HTTP {r.status_code}")
            break
        data = lire_json(r)
        if not isinstance(data, dict) or "products" not in data:
            raise PasCeType("pas Shopify")
        prods = data["products"]
        for p in prods:
            titre = p.get("title", "")
            info = analyser(titre, recherches)
            if not info:
                continue
            variantes = p.get("variants") or []
            dispo = any(v.get("available") for v in variantes)
            prix = next((v.get("price") for v in variantes if v.get("available")),
                        variantes[0].get("price") if variantes else None)
            lien = f"{base}/products/{p['handle']}"
            out.append(produit(b, lien, titre, dispo, prix, b.get("devise", "€"), info))
        if len(prods) < 250:
            break
        time.sleep(PAUSE)
    return out


# ---------------------------------------------------------------- méthode 2 : WooCommerce
def via_woo(s, b, recherches):
    url = racine_de(b["url"]) + "/wp-json/wc/store/v1/products"
    out = []
    for rech in recherches:
        for page in range(1, 11):
            r = s.get(url, params={"search": rech["requete"], "per_page": 100, "page": page},
                      timeout=TIMEOUT)
            if r.status_code != 200:
                if page == 1:
                    raise PasCeType(f"HTTP {r.status_code}")
                break
            data = lire_json(r)
            if not isinstance(data, list):
                raise PasCeType("pas WooCommerce")
            for p in data:
                titre = htmlmod.unescape(re.sub(r"<[^>]+>", "", p.get("name", "")))
                lien = p.get("permalink", "")
                info = analyser(titre + " " + slug_texte(lien), recherches)
                if not info:
                    continue
                prix_info = p.get("prices") or {}
                try:
                    prix = int(prix_info["price"]) / 10 ** int(prix_info.get("currency_minor_unit", 2))
                except (KeyError, ValueError, TypeError):
                    prix = None
                out.append(produit(b, lien, titre, bool(p.get("is_in_stock")), prix,
                                   prix_info.get("currency_symbol", "€"), info))
            if len(data) < 100:
                break
            time.sleep(PAUSE)
    return out


# ---------------------------------------------------------------- méthode 3 : pages HTML
RE_LIEN = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.S | re.I)
RE_HREF = re.compile(r"href\s*=\s*[\"']([^\"']+)[\"']", re.I)
RE_TITLE_ATTR = re.compile(r"title\s*=\s*[\"']([^\"']+)[\"']", re.I)
RE_LDJSON = re.compile(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)


def extraire_candidats(page_html, page_url, recherches):
    """Trouve dans une page de liste les liens vers des produits qui nous intéressent."""
    domaine = urlparse(page_url).netloc
    textes = {}
    for m in RE_LIEN.finditer(page_html):
        attrs, interieur = m.group(1), m.group(2)
        h = RE_HREF.search(attrs)
        if not h:
            continue
        url = urljoin(page_url, htmlmod.unescape(h.group(1))).split("#")[0]
        if urlparse(url).netloc != domaine:
            continue
        t = RE_TITLE_ATTR.search(attrs)
        txt = htmlmod.unescape(" ".join(re.sub(r"<[^>]+>", " ", interieur).split()))
        textes.setdefault(url, []).append(txt + " " + (htmlmod.unescape(t.group(1)) if t else ""))
    candidats = {}
    for url, morceaux in textes.items():
        info = analyser(" ".join(morceaux) + " " + slug_texte(url), recherches)
        if info:
            candidats[url] = info
    return candidats


def _offres(o, dispos, prix):
    if isinstance(o, list):
        for x in o:
            _offres(x, dispos, prix)
    elif isinstance(o, dict):
        if o.get("availability"):
            dispos.append(str(o["availability"]))
        p = o.get("price") or o.get("lowPrice")
        if p not in (None, ""):
            prix.append(p)
        if "offers" in o:
            _offres(o["offers"], dispos, prix)


def _produits_ld(obj, acc):
    if isinstance(obj, list):
        for x in obj:
            _produits_ld(x, acc)
        return
    if not isinstance(obj, dict):
        return
    types = obj.get("@type")
    types = types if isinstance(types, list) else [types]
    if "Product" in types or "ProductGroup" in types:
        dispos, prix = [], []
        _offres(obj.get("offers"), dispos, prix)
        for v in obj.get("hasVariant") or []:
            if isinstance(v, dict):
                _offres(v.get("offers"), dispos, prix)
        acc.append({"nom": obj.get("name"), "dispos": dispos, "prix": prix[0] if prix else None})
        return  # on ne descend pas dans les produits "similaires"
    for v in obj.values():
        if isinstance(v, (dict, list)):
            _produits_ld(v, acc)


def interpreter(valeurs):
    """['https://schema.org/InStock'] -> (True, False) ; renvoie (dispo, précommande)."""
    norm = {re.sub(r"[\s_\-]", "", v.lower().rsplit("/", 1)[-1]) for v in valeurs}
    pre = bool(norm & {"preorder", "presale"})
    if norm & DISPO_OUI:
        return True, pre
    if norm & DISPO_NON:
        return False, False
    return None, False


def lire_fiche(s, url, b):
    """Lit une fiche produit : (dispo, précommande, titre, prix). dispo=None si inconnu."""
    r = s.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    h = r.text
    produits_ld = []
    for m in RE_LDJSON.finditer(h):
        try:
            _produits_ld(json.loads(m.group(1).strip()), produits_ld)
        except ValueError:
            pass
    dispos = produits_ld[0]["dispos"] if produits_ld else []
    if not dispos:  # microdonnées et balises meta
        for tag in re.findall(r"<[^>]+itemprop=[\"']availability[\"'][^>]*>", h, re.I):
            v = re.search(r"(?:href|content)=[\"']([^\"']+)", tag)
            if v:
                dispos.append(v.group(1))
        for tag in re.findall(r"<meta[^>]+(?:product|og):availability[^>]*>", h, re.I):
            v = re.search(r"content=[\"']([^\"']+)", tag)
            if v:
                dispos.append(v.group(1))
    dispo, pre = interpreter(dispos)
    if dispo is None:  # dernier recours : textes donnés dans config.yaml pour cette boutique
        bas = h.lower()
        if b.get("texte_dispo"):
            dispo = any(t.lower() in bas for t in b["texte_dispo"])
        elif b.get("texte_rupture"):
            dispo = not any(t.lower() in bas for t in b["texte_rupture"])
    titre = produits_ld[0]["nom"] if produits_ld and produits_ld[0]["nom"] else ""
    if not titre:
        m = (re.search(r"<meta[^>]+property=[\"']og:title[\"'][^>]+content=[\"']([^\"']+)", h, re.I)
             or re.search(r"<title[^>]*>(.*?)</title>", h, re.S | re.I))
        titre = m.group(1) if m else ""
    titre = htmlmod.unescape(" ".join(str(titre).split()))
    prix = produits_ld[0]["prix"] if produits_ld else None
    return dispo, pre, titre, prix


def via_html(s, b, recherches, memo):
    candidats = {}
    if b.get("pages"):
        for page in b["pages"]:
            r = s.get(page, timeout=TIMEOUT)
            if r.status_code != 200:
                raise PasCeType(f"HTTP {r.status_code}")
            candidats.update(extraire_candidats(r.text, r.url, recherches))
            time.sleep(PAUSE)
    else:
        base = b["url"].rstrip("/")
        prefere = memo.get("modele")
        ordre = ([prefere] if prefere in MODELES_RECHERCHE else []) + \
                [x for x in MODELES_RECHERCHE if x != prefere]
        erreur = None
        for rech in recherches:
            q = quote_plus(rech["requete"])
            for modele in ordre:
                try:
                    r = s.get(base + modele.format(q=q), timeout=TIMEOUT)
                except requests.RequestException as e:
                    erreur = e.__class__.__name__
                    continue
                time.sleep(PAUSE)
                if r.status_code != 200:
                    erreur = f"HTTP {r.status_code}"
                    continue
                trouves = extraire_candidats(r.text, r.url, recherches)
                if trouves:
                    candidats.update(trouves)
                    memo["modele"] = modele
                    ordre = [modele] + [x for x in ordre if x != modele]
                    break
        if not candidats and erreur and erreur.startswith("HTTP 403"):
            raise PasCeType(erreur)

    out = []
    for url, info in list(candidats.items())[:MAX_FICHES]:
        try:
            dispo, pre, titre, prix = lire_fiche(s, url, b)
        except requests.RequestException:
            continue
        time.sleep(PAUSE)
        if titre:  # le titre complet de la fiche précise souvent la langue
            if RE_EXCLU.search(titre):
                continue
            info = analyser(titre + " " + slug_texte(url), recherches) or info
        out.append(produit(b, url, titre or url, dispo, prix, "€", info, pre))
    return out


# ---------------------------------------------------------------- une boutique
def scanner(b, recherches, memo):
    s = requests.Session()
    s.headers.update(UA)
    ordre = ["shopify", "woo", "html"]
    if b.get("pages"):
        ordre = ["html"]
    elif memo.get("methode") in ordre:
        ordre.remove(memo["methode"])
        ordre.insert(0, memo["methode"])
    raisons = []
    for methode in ordre:
        try:
            if methode == "shopify":
                prods = via_shopify(s, b, recherches)
            elif methode == "woo":
                prods = via_woo(s, b, recherches)
            else:
                prods = via_html(s, b, recherches, memo)
            memo["methode"] = methode
            return {"nom": b["nom"], "methode": methode, "produits": prods, "erreur": None}
        except PasCeType as e:
            raisons.append(f"{methode} {e}")
        except requests.RequestException as e:
            raisons.append(f"{methode} {e.__class__.__name__}")
    return {"nom": b["nom"], "methode": None, "produits": [],
            "erreur": raisons[-1] if raisons else "inconnue"}


# ---------------------------------------------------------------- Telegram
def message(p):
    entete = "🕒 PRÉCOMMANDE OUVERTE" if p["precommande"] else "EN STOCK"
    lignes = [f"{EMOJI.get(p['type'], '🔔')} {entete} — {p['boutique']}", p["titre"]]
    details = [p["set"], p["type"], p["langue"].upper() if p["langue"] != "?" else "",
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
                      data={"chat_id": chat, "text": texte[:4000],
                            "disable_web_page_preview": "true"}, timeout=20)
    if not r.ok:
        print("Erreur Telegram :", r.text)


def rapport(resultats):
    lignes = ["📋 Rapport boutiques"]
    for res in resultats:
        if res["erreur"]:
            lignes.append(f"❌ {res['nom']} : bloqué ou injoignable ({res['erreur']})")
            continue
        prods = res["produits"]
        if not prods:
            lignes.append(f"⚪ {res['nom']} [{res['methode']}] : aucun produit trouvé")
            continue
        en_stock = sum(1 for p in prods if p["dispo"])
        inconnus = sum(1 for p in prods if p["dispo"] is None)
        ligne = f"✅ {res['nom']} [{res['methode']}] : {len(prods)} produits, {en_stock} en stock"
        if inconnus:
            ligne += f", {inconnus} stock illisible"
        lignes.append(ligne)
    return "\n".join(lignes)


# ---------------------------------------------------------------- programme principal
def main():
    with open(FICHIER_CONFIG, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    recherches = cfg["recherches"]
    for r in recherches:
        r["_motif"] = re.compile(r["motif_set"], re.I)

    etat = {}
    if os.path.exists(FICHIER_ETAT):
        with open(FICHIER_ETAT, encoding="utf-8") as f:
            etat = json.load(f)
    if "produits" not in etat:  # ancien format (version 1)
        etat = {"produits": {}, "methodes": {}, "boutiques_vues": []}
    etat.setdefault("methodes", {})
    etat.setdefault("boutiques_vues", [])
    premier_lancement = not etat["produits"]

    boutiques = cfg.get("boutiques") or []
    memos = [etat["methodes"].setdefault(b["nom"], {}) for b in boutiques]
    with ThreadPoolExecutor(max_workers=6) as ex:
        resultats = list(ex.map(lambda bm: scanner(bm[0], recherches, bm[1]),
                                zip(boutiques, memos)))

    alertes = []
    for res in resultats:
        nouvelle_boutique = res["nom"] not in etat["boutiques_vues"]
        for p in res["produits"]:
            if p["dispo"] is None:
                continue
            avant = etat["produits"].get(p["cle"])
            if p["dispo"] and avant is not True and not premier_lancement and not nouvelle_boutique:
                alertes.append(p)
            etat["produits"][p["cle"]] = p["dispo"]
        if not res["erreur"] and res["nom"] not in etat["boutiques_vues"]:
            etat["boutiques_vues"].append(res["nom"])

    if premier_lancement or os.environ.get("RAPPORT") == "1":
        envoyer(rapport(resultats))
    for p in alertes:
        envoyer(message(p))

    with open(FICHIER_ETAT, "w", encoding="utf-8") as f:
        json.dump(etat, f, ensure_ascii=False, indent=1, sort_keys=True)
    print(rapport(resultats))
    print(f"{len(alertes)} alerte(s) envoyée(s)")


if __name__ == "__main__":
    main()
