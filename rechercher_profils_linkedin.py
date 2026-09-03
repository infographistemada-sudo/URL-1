# -*- coding: utf-8 -*-
import os
import re
import time
import random
import threading
import unicodedata
import pandas as pd

# Import natif basé sur votre exemple de script
try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

# ==========================================
# CONFIGURATION
# ==========================================
FICHIER_ENTREE = "liste_urls.csv"
FICHIER_SORTIE = "profils_linkedin_trouves.csv"

POSTES_CIBLES = [
    "Directeur d'établissement",
    "Manager d'établissement",
    "Directeur équipements",
    "Responsable équipements"
]

# Nombre d'entreprises traitées par exécution (utile pour GitHub Actions,
# afin de rester sous la limite de temps d'un job et de relancer en boucle).
# BATCH_SIZE=0 (ou variable absente) => traite tout le fichier en une seule fois.
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "0") or "0")

# Mots indiquant que le poste n'est probablement plus d'actualité
INDICES_ANCIEN_POSTE = ["ancien", "ex-", "ex ", "former", "etait", "a quitte", "ancienne"]

WRITE_LOCK = threading.Lock()

# ==========================================
# FONCTIONS UTILITAIRES (Inspirées de votre exemple)
# ==========================================

def read_table_with_format(path):
    """Lit le fichier CSV avec détection automatique des encodages et séparateurs,
    et renvoie aussi l'encodage/séparateur détectés (pour pouvoir réécrire le fichier
    dans le même format)."""
    trials = [("utf-8", ","), ("utf-8", ";"), ("utf-8", "\t"), ("utf-8-sig", ","), ("utf-8-sig", ";"),
              ("utf-8-sig", "\t"), ("cp1252", ","), ("cp1252", ";"), ("cp1252", "\t")]
    for enc, sep in trials:
        try:
            df = pd.read_csv(path, encoding=enc, sep=sep)
            if len(df.columns) >= 1:
                return df, enc, sep
        except Exception:
            pass
    raise ValueError(f"Impossible de lire le fichier : {path}")

def read_table(path):
    """Lit le fichier CSV avec détection automatique des encodages et séparateurs (Votre fonction)."""
    df, _enc, _sep = read_table_with_format(path)
    return df

def extraire_nom_entreprise(url):
    """Extrait le nom de l'entreprise depuis l'URL LinkedIn."""
    if not isinstance(url, str):
        return None
    url = url.strip().rstrip('/')
    match = re.search(r'/(?:company|school)/([^/?#]+)', url)
    if match:
        return match.group(1).replace('-', ' ').title()
    return None

def normaliser(texte):
    """Met un texte en minuscule, sans accents ni ponctuation, pour comparaison robuste."""
    if not texte:
        return ""
    texte = str(texte).lower()
    texte = unicodedata.normalize('NFKD', texte).encode('ascii', 'ignore').decode('ascii')
    texte = re.sub(r'[^a-z0-9\s]', ' ', texte)
    texte = re.sub(r'\s+', ' ', texte).strip()
    return texte

def clean_profile_title(title):
    """Nettoie le titre pour isoler au mieux le Prénom Nom."""
    if not title:
        return "Inconnu"
    title = re.sub(r'\s*\|\s*LinkedIn.*', '', title, flags=re.IGNORECASE)
    parts = title.split('-')
    if parts:
        return parts[0].strip()
    return title.strip()

def extraire_intitule_reel(title):
    """
    Extrait l'intitulé de poste RÉEL de la personne à partir du titre du résultat
    de recherche (et non le poste recherché par la requête).

    Format typique LinkedIn :
      "Prénom Nom - Intitulé de poste - Entreprise | LinkedIn"
    Le 2e segment (après le 1er tiret) correspond à l'intitulé réel du poste.
    """
    if not title:
        return ""
    t = re.sub(r'\s*\|\s*LinkedIn.*', '', title, flags=re.IGNORECASE)
    parts = [p.strip() for p in t.split('-') if p.strip()]
    if len(parts) >= 2:
        return parts[1]
    return ""

def extraire_entreprise_reelle(title):
    """
    Extrait le nom de l'ENTREPRISE réelle où travaille la personne, à partir du titre
    du résultat de recherche (3e segment, quand présent) :
      "Prénom Nom - Intitulé de poste - Entreprise | LinkedIn"
    """
    if not title:
        return ""
    t = re.sub(r'\s*\|\s*LinkedIn.*', '', title, flags=re.IGNORECASE)
    parts = [p.strip() for p in t.split('-') if p.strip()]
    if len(parts) >= 3:
        # Au cas où le nom d'entreprise contienne lui-même un tiret, on recolle le reste
        return '-'.join(parts[2:]).strip()
    return ""

def entreprise_correspond(entreprise_cible, entreprise_extraite):
    """Vérifie si l'entreprise extraite correspond à l'entreprise recherchée (comparaison souple)."""
    if not entreprise_extraite:
        return False
    cible_norm = normaliser(entreprise_cible)
    extraite_norm = normaliser(entreprise_extraite)
    if not cible_norm or not extraite_norm:
        return False
    if cible_norm in extraite_norm or extraite_norm in cible_norm:
        return True
    mots_cible = {m for m in cible_norm.split() if len(m) >= 4}
    mots_extraite = {m for m in extraite_norm.split() if len(m) >= 4}
    return bool(mots_cible & mots_extraite)

def entreprise_dans_body(body, nom_entreprise):
    """Vérifie si le nom de l'entreprise recherchée apparaît dans la description du résultat."""
    if not body:
        return False
    body_norm = normaliser(body)
    cible_norm = normaliser(nom_entreprise)
    if not cible_norm:
        return False
    mots_cible = {m for m in cible_norm.split() if len(m) >= 4}
    if not mots_cible:
        return cible_norm in body_norm
    return any(m in body_norm for m in mots_cible)

def verifier_emploi_actuel(nom_entreprise, entreprise_reelle, body):
    """
    Estime si la personne travaille ENCORE aujourd'hui dans l'entreprise recherchée.
    Se base sur les données publiques indexées (titre + description du résultat) :
    ce n'est PAS une vérification en temps réel du profil LinkedIn (nécessiterait
    une connexion authentifiée), mais une estimation à partir de ce qui est indexé.
    Retourne (bool_emploi_actuel, raison_texte).
    """
    texte_combine_norm = normaliser(f"{entreprise_reelle} {body}")

    if any(mot in texte_combine_norm for mot in INDICES_ANCIEN_POSTE):
        return False, "Non - indice d'ancien poste detecte"

    if entreprise_reelle and entreprise_correspond(nom_entreprise, entreprise_reelle):
        return True, "Oui - entreprise confirmee dans le titre du profil"

    if entreprise_dans_body(body, nom_entreprise):
        return True, "Oui (probable) - entreprise mentionnee dans la description"

    return False, "Non confirme - entreprise non retrouvee"

def marquer_traite(colonne_url, url_traitee, statut, enc, sep):
    """
    Marque une ligne du fichier D'ENTREE (liste_urls.csv) comme traitée, dans une
    colonne "Traite", et réécrit immédiatement le fichier (dans son format d'origine).
    Permet une reprise fiable même si le job GitHub Actions s'arrête entre deux lots.
    """
    with WRITE_LOCK:
        try:
            df_actuel = pd.read_csv(FICHIER_ENTREE, encoding=enc, sep=sep)
        except Exception:
            df_actuel = read_table(FICHIER_ENTREE)

        if "Traite" not in df_actuel.columns:
            df_actuel["Traite"] = ""

        masque = df_actuel[colonne_url].astype(str).str.strip() == url_traitee.strip()
        df_actuel.loc[masque, "Traite"] = statut
        df_actuel.to_csv(FICHIER_ENTREE, index=False, sep=sep, encoding=enc)

def search_duckduckgo_direct(nom_entreprise, poste, max_results=5, max_retries=3):
    """
    Effectue une recherche directe (Méthode de votre exemple).
    Pas de X-Ray strict, on demande les mots clés naturellement.
    """
    query = f"{nom_entreprise} {poste} linkedin"

    for attempt in range(1, max_retries + 1):
        try:
            results = []
            with DDGS() as ddgs:
                # Utilisation de la méthode de texte directe comme dans votre exemple
                ddg_generator = ddgs.text(query, region="fr-fr", max_results=max_results)
                for item in ddg_generator:
                    url = item.get("href", "")
                    # Filtre corrigé : accepte tous les sous-domaines (fr., www., ca., etc.)
                    # et cible spécifiquement les profils personnels (/in/), pas les pages entreprise
                    if "linkedin.com/in/" in url:
                        results.append({
                            "title": item.get("title", ""),
                            "url": url,
                            "body": item.get("body", "")
                        })
            return results
        except Exception as e:
            print(f"    ⚠️ Erreur DDG (tentative {attempt}/{max_retries}) : {e}")
            time.sleep(attempt * random.uniform(2.0, 4.0))
    return []

# ==========================================
# SCRIPT PRINCIPAL
# ==========================================

def main():
    if not os.path.exists(FICHIER_ENTREE):
        print(f"❌ Erreur : Le fichier d'entrée '{FICHIER_ENTREE}' est introuvable.")
        return

    # 1. Chargement et normalisation des données sources
    df_entree, enc_entree, sep_entree = read_table_with_format(FICHIER_ENTREE)
    colonne_url = None
    for col in df_entree.columns:
        if df_entree[col].astype(str).str.contains("linkedin.com", na=False).any():
            colonne_url = col
            break

    if not colonne_url:
        colonne_url = df_entree.columns[0]

    if "Traite" not in df_entree.columns:
        df_entree["Traite"] = ""
        df_entree.to_csv(FICHIER_ENTREE, index=False, sep=sep_entree, encoding=enc_entree)

    urls_a_traiter = df_entree[colonne_url].dropna().unique().tolist()
    print(f"🚀 {len(urls_a_traiter)} URL(s) détectée(s) dans '{FICHIER_ENTREE}'.")

    # 2. Gestion de la reprise après plantage : on croise 2 sources d'information
    #    a) la colonne "Traite" du fichier d'entrée liste_urls.csv
    #    b) le fichier de sortie déjà généré
    urls_deja_marquees = set(
        df_entree.loc[df_entree["Traite"].astype(str).str.strip() != "", colonne_url]
        .dropna().unique().tolist()
    )

    urls_deja_traitees = set()
    if os.path.exists(FICHIER_SORTIE):
        try:
            df_existant = read_table(FICHIER_SORTIE)
            if "URL Entreprise" in df_existant.columns:
                urls_deja_traitees = set(df_existant["URL Entreprise"].dropna().unique().tolist())
        except Exception:
            pass

    urls_deja_traitees |= urls_deja_marquees
    print(f"ℹ️ Reprise active : {len(urls_deja_traitees)} URL(s) déjà traitée(s) ignorée(s).")

    # 2bis. On ne garde que les URLs restant à traiter
    urls_restantes = [u for u in urls_a_traiter if u.strip() not in urls_deja_traitees]

    if BATCH_SIZE > 0:
        urls_du_lot = urls_restantes[:BATCH_SIZE]
        print(f"📦 Mode lot activé (BATCH_SIZE={BATCH_SIZE}) : {len(urls_du_lot)} URL(s) traitée(s) sur {len(urls_restantes)} restante(s).")
    else:
        urls_du_lot = urls_restantes

    # 3. Traitement séquentiel (1 à 1)
    for index, url in enumerate(urls_du_lot, 1):
        url_clean = url.strip()

        nom_entreprise = extraire_nom_entreprise(url_clean)
        if not nom_entreprise:
            print(f"[{index}/{len(urls_du_lot)}] URL non valide : {url_clean}")
            marquer_traite(colonne_url, url_clean, "Invalide", enc_entree, sep_entree)
            continue

        print(f"[{index}/{len(urls_du_lot)}] Recherche directe pour : {nom_entreprise}...")
        profils_trouves = []
        profils_ecartes = 0
        urls_uniques_profils = set()

        # Itération sur chaque poste
        for poste in POSTES_CIBLES:
            # Temporisation pour ne pas surcharger DuckDuckGo (comme dans votre exemple)
            time.sleep(random.uniform(2.0, 4.0))

            resultats_recherche = search_duckduckgo_direct(nom_entreprise, poste)

            for res in resultats_recherche:
                profil_url = res["url"]
                if profil_url in urls_uniques_profils:
                    continue
                urls_uniques_profils.add(profil_url)

                nom_prenom = clean_profile_title(res["title"])
                intitule_reel = extraire_intitule_reel(res["title"])
                entreprise_reelle = extraire_entreprise_reelle(res["title"])
                emploi_actuel, raison = verifier_emploi_actuel(nom_entreprise, entreprise_reelle, res.get("body", ""))

                # On ne garde QUE les profils dont on estime qu'ils travaillent
                # ENCORE aujourd'hui dans l'entreprise recherchée.
                if not emploi_actuel:
                    profils_ecartes += 1
                    continue

                profils_trouves.append((nom_prenom, profil_url, poste, intitule_reel, entreprise_reelle, raison))

        # 4. Préparation de la ligne finale
        row_data = {
            "URL Entreprise": url_clean,
            "Nom Entreprise": nom_entreprise,
            "Statut Traitement": "Traite - Profil Trouve" if profils_trouves else "Traite - Aucun profil trouve"
        }

        # Alignement en colonnes (Collaborateur 1, Collaborateur 2...)
        for idx, (nom_prenom, p_url, poste, intitule_reel, entreprise_reelle, raison) in enumerate(profils_trouves, 1):
            row_data[f"Poste Recherche {idx}"] = poste
            row_data[f"Intitule Reel {idx}"] = intitule_reel
            row_data[f"Entreprise Actuelle {idx}"] = entreprise_reelle or nom_entreprise
            row_data[f"Collaborateur {idx}"] = nom_prenom
            row_data[f"Lien LinkedIn {idx}"] = p_url
            row_data[f"Verification Emploi {idx}"] = raison

        df_nouvelle_ligne = pd.DataFrame([row_data])

        # 5. Écriture thread-safe en temps réel
        with WRITE_LOCK:
            if os.path.exists(FICHIER_SORTIE):
                try:
                    df_existant = read_table(FICHIER_SORTIE)
                    df_final = pd.concat([df_existant, df_nouvelle_ligne], ignore_index=True)
                except Exception:
                    df_final = df_nouvelle_ligne
            else:
                df_final = df_nouvelle_ligne

            df_final.to_csv(FICHIER_SORTIE, index=False, sep=";", encoding="utf-8-sig")

        # Marque la ligne comme traitée dans liste_urls.csv (reprise fiable entre lots)
        marquer_traite(colonne_url, url_clean, "Oui", enc_entree, sep_entree)

        print(f"  ✅ {len(profils_trouves)} profil(s) retenu(s) (emploi actuel confirmé) "
              f"— {profils_ecartes} écarté(s) (entreprise différente ou non confirmée) pour {nom_entreprise}.")

    restant_apres_lot = len(urls_restantes) - len(urls_du_lot)
    print(f"\n🎉 Script terminé pour ce lot. Fichier mis à jour : '{FICHIER_SORTIE}'")
    print(f"📊 Il reste {restant_apres_lot} URL(s) à traiter.")

    # Écrit un indicateur simple pour que le workflow GitHub Actions sache
    # s'il doit se relancer automatiquement.
    with open("reste_a_traiter.txt", "w", encoding="utf-8") as f:
        f.write(str(restant_apres_lot))

if __name__ == "__main__":
    main()
