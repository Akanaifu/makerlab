from pathlib import Path
import re
import json
import subprocess
from typing import Any
import os


# Exception levée quand l'utilisateur demande de revenir au menu principal
class RetourMenu(Exception):
    pass


def input_retour(prompt: str) -> str:
    """Lit une entrée utilisateur; lève `RetourMenu` si l'utilisateur saisit 'retour'."""
    s = input(prompt).strip()
    if s.lower() in ("retour", "r"):
        raise RetourMenu()
    return s


CONFIG_FILE: Path = "./pcb2gcode_config.json"


def charger_config() -> dict[str, Any]:
    """
    Charge la configuration depuis le fichier JSON,
    """
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def sauvegarder_config(config: dict[str, Any]) -> None:
    """Sauvegarde la configuration dans le fichier JSON."""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4, ensure_ascii=False)
    print("Configuration sauvegardée.\n")


def afficher_parametres(config: dict[str, Any]) -> None:
    """Affiche les paramètres actuels de la configuration."""
    print("\n--- Paramètres actuels ---")
    print(f"  pcb2gcode_exe: {config['pcb2gcode_exe']}")
    for cle, val in config["parametres"].items():
        print(f"  {cle}: {val}")
    print()


def lire_choix_parametre() -> str:
    """Lit le nom du paramètre à modifier."""
    return input_retour(
        "Entrez le nom du paramètre à modifier (ou 'retour' pour revenir):\n> "
    )


def lire_nouvelle_valeur(libelle: str, valeur_actuelle: Any) -> str:
    """Lit une nouvelle valeur utilisateur pour un paramètre donné."""
    return input_retour(f"  Nouvelle valeur pour {libelle} [{valeur_actuelle}]: ")


def convertir_valeur_parametre(ancienne_valeur: Any, nouvelle_valeur: str) -> Any:
    """Convertit la valeur saisie vers le type attendu."""
    if isinstance(ancienne_valeur, bool):
        return nouvelle_valeur.lower() in ("true", "1", "oui")
    if isinstance(ancienne_valeur, int):
        return int(nouvelle_valeur)
    if isinstance(ancienne_valeur, float):
        return float(nouvelle_valeur)
    return nouvelle_valeur


def modifier_parametres(config: dict[str, Any]) -> None:
    """Permet à l'utilisateur de modifier les paramètres de la configuration."""
    while True:
        afficher_parametres(config)
        choix: str = lire_choix_parametre()
        if choix == "retour":
            break
        if choix == "pcb2gcode_exe":
            nouvelle_val: str = lire_nouvelle_valeur(
                "pcb2gcode_exe", config["pcb2gcode_exe"]
            )
            if nouvelle_val:
                config["pcb2gcode_exe"] = nouvelle_val
                sauvegarder_config(config)
        elif choix in config["parametres"]:
            ancienne = config["parametres"][choix]
            nouvelle_val: str = lire_nouvelle_valeur(choix, ancienne)
            if nouvelle_val:
                config["parametres"][choix] = convertir_valeur_parametre(
                    ancienne, nouvelle_val
                )
                sauvegarder_config(config)
        else:
            print(f"  Paramètre '{choix}' inconnu.")


# Commandes G-code incompatibles avec GRBL
GRBL_INCOMPATIBLE: re.Pattern[str] = re.compile(
    r"^\s*(G64\b|M6\b|M0\b|G04\s+P0\s*\(.*G64)"
)

# Options pcb2gcode qui se comportent comme des flags (présence/absence)
FLAG_OPTIONS: set[str] = {
    "metric",
    "metricoutput",
    "vectorial",
    "voronoi",
    "zero-start",
}


def fichier_drl_contient_percages(chemin_drl: str | Path) -> bool:
    """Retourne True si le fichier Excellon contient au moins un perçage exploitable."""
    p = Path(chemin_drl)
    if not p.is_file():
        return False

    pattern_coord: re.Pattern[str] = re.compile(r"^[XY]-?\d")
    with p.open("r", encoding="utf-8", errors="ignore") as f:
        for ligne in f:
            s = ligne.strip()
            if not s or s.startswith(";"):
                continue
            if pattern_coord.match(s):
                return True
    return False


def gerber_contient_pistes(chemin_gerber: str | Path) -> bool:
    """Retourne True si le Gerber contient de vraies instructions de tracé (D01)."""
    p = Path(chemin_gerber)
    if not p.is_file():
        return False

    with p.open("r", encoding="utf-8", errors="ignore") as f:
        for ligne in f:
            s = ligne.strip()
            if "D01" in s:
                return True
    return False


def nettoyer_gcode_grbl(dossier_output: str | Path) -> None:
    """Supprime les commandes non supportées par GRBL des fichiers .ngc."""
    print("\nNettoyage G-code pour compatibilité GRBL...")
    d = Path(dossier_output)
    if not d.is_dir():
        print(f"  Dossier '{dossier_output}' introuvable.")
        return

    fichiers = [p for p in d.iterdir() if p.suffix == ".ngc"]
    if not fichiers:
        print("  Aucun fichier .ngc trouvé.")
        return

    for chemin in fichiers:
        with chemin.open("r", encoding="utf-8", errors="ignore") as f:
            lignes = f.readlines()
        lignes_propres = [l for l in lignes if not GRBL_INCOMPATIBLE.match(l)]
        supprimees = len(lignes) - len(lignes_propres)
        with chemin.open("w", encoding="utf-8") as f:
            f.writelines(lignes_propres)
        print(f"  [OK] {chemin.name} ({supprimees} lignes supprimées)")


def lire_fichier_si_existe(chemin: str | Path) -> Path | None:
    """Retourne le Path si le fichier existe, sinon None."""
    p = Path(chemin)
    return p if p.is_file() else None


def valeur_bool(v: Any) -> bool:
    """Interprète différentes formes de booléens (bool, int, str)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "oui", "o", "yes", "on")
    return bool(v)


def construire_commande(
    config: dict[str, Any],
    front: Path | None,
    back: Path | None,
    drill: Path | None,
    output: Path,
) -> list[str]:
    """
    Construit la liste de commandes à passer à subprocess.run()
    en fonction de la configuration et des chemins des fichiers.
    """
    cmd = [
        config["pcb2gcode_exe"],
        "--noconfigfile",
        f"--output-dir={output}",
    ]

    if front:
        cmd.append(f"--front={front}")
    if back:
        cmd.append(f"--back={back}")
    if drill:
        cmd.append(f"--drill={drill}")

    # Copie locale pour pouvoir normaliser des dépendances d'options.
    parametres = dict(config["parametres"])

    vectorial_actif = valeur_bool(parametres.get("vectorial", 0))
    voronoi_actif = valeur_bool(parametres.get("voronoi", 0))

    if not vectorial_actif and voronoi_actif:
        print("[INFO] voronoi désactivé: nécessite vectorial.")
        parametres["voronoi"] = 0

    # En mode classique, pcb2gcode requiert --offset.
    if not vectorial_actif and "offset" not in parametres:
        cutter = float(parametres.get("cutter-diameter", 0.1))
        parametres["offset"] = round(cutter / 2.0, 5)
        print(
            f"[INFO] offset absent -> utilisation automatique de {parametres['offset']}"
        )

    if "g64" in parametres and "tolerance" not in parametres:
        parametres["tolerance"] = parametres.pop("g64")
        print("[INFO] g64 est obsolète -> conversion automatique vers tolerance.")

    for cle, val in parametres.items():
        if isinstance(val, bool) or cle in FLAG_OPTIONS:
            if valeur_bool(val):
                cmd.append(f"--{cle}")
        else:
            cmd.append(f"--{cle}={val}")
    return cmd


def demander_dimensions_pcb() -> tuple[float, float]:
    """Demande les dimensions du PCB."""
    largeur_pcb = float(input_retour("Largeur du pcb (en mm): "))
    hauteur_pcb = float(input_retour("Longueur du pcb (en mm): "))
    return largeur_pcb, hauteur_pcb


def construire_chemins_gerber(
    dossier: str | Path, nom: str
) -> tuple[Path, Path, Path, Path]:
    """Construit les chemins des fichiers Gerber attendus."""
    p = Path(dossier)
    front = p / f"{nom}-F_Cu.gbr"
    back = p / f"{nom}-B_Cu.gbr"
    drill = p / f"{nom}.drl"
    output = p / "output"
    return front, back, drill, output


def trouver_fichier_edge_cuts(dossier: Path) -> Path | None:
    """Retourne le fichier Edge.Cuts du projet s'il existe."""
    candidats = sorted(
        [
            p
            for p in dossier.iterdir()
            if p.is_file() and p.name.lower().endswith("edge_cuts.gbr")
        ]
    )
    return candidats[0] if candidats else None


def extraire_echelle_gerber(chemin_edge_cuts: Path) -> int:
    """Extrait le nombre de décimales du format Gerber."""
    patron_format = re.compile(r"%FS[LT][AI]X\d(\d)Y\d(\d)\*%")
    with chemin_edge_cuts.open("r", encoding="utf-8", errors="ignore") as f:
        for ligne in f:
            match = patron_format.search(ligne.strip())
            if match:
                return max(int(match.group(1)), int(match.group(2)))
    return 6


def parser_coordonnees_gerber(
    chemin_edge_cuts: Path,
) -> list[tuple[float, float]]:
    """Lit les coordonnées tracées d'un fichier Gerber de contour."""
    echelle = extraire_echelle_gerber(chemin_edge_cuts)
    facteur = 10**echelle
    patron_coordonnees = re.compile(
        r"^(?:X(?P<x>-?\d+))?(?:Y(?P<y>-?\d+))?D0(?P<operation>[12])\*$"
    )

    points: list[tuple[float, float]] = []
    courant_x: float | None = None
    courant_y: float | None = None

    with chemin_edge_cuts.open("r", encoding="utf-8", errors="ignore") as f:
        for ligne in f:
            texte = ligne.strip()
            match = patron_coordonnees.match(texte)
            if not match:
                continue
            if match.group("x") is not None:
                courant_x = int(match.group("x")) / facteur
            if match.group("y") is not None:
                courant_y = int(match.group("y")) / facteur
            if courant_x is not None and courant_y is not None:
                points.append((courant_x, courant_y))

    return points


def extraire_dimensions_depuis_edge_cuts(
    chemin_edge_cuts: Path,
) -> tuple[float, float] | None:
    """Calcule largeur et hauteur à partir d'un fichier Edge.Cuts."""
    points = parser_coordonnees_gerber(chemin_edge_cuts)
    if not points:
        return None

    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    largeur = round(max(xs) - min(xs), 4)
    hauteur = round(max(ys) - min(ys), 4)
    return largeur, hauteur


def extraire_echelle_coordonnees(chemin_source: Path, valeur_defaut: int = 6) -> int:
    """Extrait l'échelle d'un fichier Gerber/Excellon, sinon renvoie une valeur par défaut."""
    patron_format = re.compile(r"%FS[LT][AI]X\d(\d)Y\d(\d)\*%")
    with chemin_source.open("r", encoding="utf-8", errors="ignore") as f:
        for ligne in f:
            match = patron_format.search(ligne.strip())
            if match:
                return max(int(match.group(1)), int(match.group(2)))
    return valeur_defaut


def translater_ligne_coordonnees(
    ligne: str,
    delta_x: int,
    delta_y: int,
) -> str:
    """Translate les coordonnées X/Y d'une ligne de Gerber ou d'Excellon."""
    patron_coord = re.compile(r"(?P<axe>[XY])(?P<valeur>-?\d+)")

    def remplacer(match: re.Match[str]) -> str:
        axe = match.group("axe")
        valeur = int(match.group("valeur"))
        if axe == "X":
            return f"X{valeur + delta_x}"
        return f"Y{valeur + delta_y}"

    if "X" not in ligne and "Y" not in ligne:
        return ligne
    return patron_coord.sub(remplacer, ligne)


def copier_fichier_avec_offset(
    source: Path,
    destination: Path,
    offset_x_mm: float,
    offset_y_mm: float,
) -> None:
    """Copie un fichier Gerber/Excellon en décalant ses coordonnées."""
    echelle = extraire_echelle_coordonnees(source)
    delta_x = int(round(offset_x_mm * (10**echelle)))
    delta_y = int(round(offset_y_mm * (10**echelle)))

    with source.open(
        "r", encoding="utf-8", errors="ignore"
    ) as entree, destination.open("w", encoding="utf-8") as sortie:
        for ligne in entree:
            sortie.write(translater_ligne_coordonnees(ligne, delta_x, delta_y))


def preparer_gerbers_avec_offset(
    dossier_source: Path,
    front: Path | None,
    back: Path | None,
    drill: Path | None,
    dossier_travail: Path,
    offset_x_mm: float,
    offset_y_mm: float,
) -> tuple[Path | None, Path | None, Path | None]:
    """Crée des copies décalées des fichiers Gerber utilisés par pcb2gcode."""
    if offset_x_mm == 0 and offset_y_mm == 0:
        return front, back, drill

    dossier_offset = dossier_travail / "gerbers_offset"
    dossier_offset.mkdir(parents=True, exist_ok=True)

    fichiers_source = [
        p
        for p in dossier_source.iterdir()
        if p.is_file() and p.suffix.lower() in {".gbr", ".drl"}
    ]
    for source in fichiers_source:
        destination = dossier_offset / source.name
        copier_fichier_avec_offset(source, destination, offset_x_mm, offset_y_mm)

    front_offset = dossier_offset / front.name if front else None
    back_offset = dossier_offset / back.name if back else None
    drill_offset = dossier_offset / drill.name if drill else None
    return front_offset, back_offset, drill_offset


def demander_offset_gerber() -> tuple[float, float]:
    """Demande un décalage X/Y en mm pour les fichiers Gerber."""
    offset_x_texte = input_retour("Offset X en mm (défaut 0): ").strip()
    offset_y_texte = input_retour("Offset Y en mm (défaut 0): ").strip()
    offset_x = float(offset_x_texte) if offset_x_texte else 0.0
    offset_y = float(offset_y_texte) if offset_y_texte else 0.0
    return offset_x, offset_y


def lecture_dossier_de_dossier(
    config: dict[str, Any],
    chemin_dossier: str | Path,
    offset_x_mm: float = 0.0,
    offset_y_mm: float = 0.0,
) -> list[str] | None:
    """
    extrait tous les sous-dossiers d'un dossier donné
    """

    p = Path(chemin_dossier)
    if not p.is_dir():
        print(f"Erreur : le chemin_dossier '{chemin_dossier}' n'existe pas.")
        return
    list_name_dir: list[str] = []
    for subdir in p.iterdir():
        if subdir.is_dir():
            lancer(config, subdir, offset_x_mm=offset_x_mm, offset_y_mm=offset_y_mm)
            list_name_dir.append(subdir.name)

    return list_name_dir


def lecture_chemin_dossier(
    chemin_dossier: str | Path,
) -> tuple[Path, Path, Path, Path, float, float] | None:
    """
    extraction des fichiers gerber
    """
    p = Path(chemin_dossier)
    nom = p.name
    if not p.is_dir():
        print(f"Erreur : le chemin_dossier '{chemin_dossier}' n'existe pas.")
        return
    print(f"============={nom}=============")
    edge_cuts = trouver_fichier_edge_cuts(p)
    if edge_cuts is not None:
        dimensions = extraire_dimensions_depuis_edge_cuts(edge_cuts)
        if dimensions is not None:
            largeur_pcb, hauteur_pcb = dimensions
            print(
                f"Dimensions extraites depuis {edge_cuts.name}: {largeur_pcb} mm x {hauteur_pcb} mm"
            )
        else:
            print(
                f"[INFO] Impossible d'extraire les dimensions depuis {edge_cuts.name}, saisie manuelle demandée."
            )
            largeur_pcb, hauteur_pcb = demander_dimensions_pcb()
    else:
        largeur_pcb, hauteur_pcb = demander_dimensions_pcb()
    front, back, drill, output = construire_chemins_gerber(p, nom)
    return front, back, drill, output, hauteur_pcb, largeur_pcb


def verifier_fichiers_entree(
    front: Path, back: Path, drill: Path
) -> tuple[Path | None, Path | None, Path | None]:
    """Vérifie quels fichiers d'entrée existent réellement."""
    front_effectif = lire_fichier_si_existe(front)
    back_effectif = lire_fichier_si_existe(back)
    drill_effectif = lire_fichier_si_existe(drill)

    for label, chemin in [
        ("Front", front_effectif or front),
        ("Back", back_effectif or back),
        ("Drill", drill_effectif or drill),
    ]:
        if Path(chemin).is_file():
            print(f"  [OK] {label}: {chemin}")
        else:
            print(f"  [MANQUANT] {label}: {chemin}")

    return front_effectif, back_effectif, drill_effectif


def filtrer_pistes_back(back_effectif: Path | None) -> Path | None:
    """Ignore la couche back si elle ne contient pas de pistes."""
    if back_effectif and not gerber_contient_pistes(back_effectif):
        print("[INFO] Couche back vide detectee -> option --back ignoree.")
        return None
    return back_effectif


def filtrer_percages_drill(drill_effectif: Path | None) -> Path | None:
    """Ignore le fichier drill si aucun perçage n'est détecté."""
    if drill_effectif and not fichier_drl_contient_percages(drill_effectif):
        print("[INFO] Fichier drill sans perçages detectes -> option --drill ignoree.")
        return None
    return drill_effectif


def afficher_aucune_entree_si_besoin(
    front: Path | None, back: Path | None, drill: Path | None
) -> bool:
    """Retourne True si aucun fichier d'entrée n'est disponible."""
    if not any([front, back, drill]):
        print("\nAucun fichier d'entree disponible. Conversion annulee.")
        return True
    return False


def preparer_dossier_sortie(output: str | Path) -> Path:
    """Crée le dossier de sortie s'il n'existe pas."""
    output_path = Path(output)
    output_path.mkdir(parents=True, exist_ok=True)
    return output_path


def resoudre_executable_pcb2gcode(config: dict[str, Any]) -> str:
    """Garantit que le chemin de pcb2gcode pointe vers un fichier existant."""
    pcb2gcode_exe = str(config.get("pcb2gcode_exe", "")).strip()

    # Tant que le chemin fourni n'est pas un fichier existant, redemander.
    while not Path(pcb2gcode_exe).is_file():
        pcb2gcode_exe = input_retour(
            f"\npcb2gcode.exe introuvable à '{pcb2gcode_exe}'.\nChemin complet: "
        ).strip()
        if not pcb2gcode_exe:
            print("Chemin vide — veuillez saisir le chemin complet vers pcb2gcode.exe.")
            continue

        # Sauvegarder la nouvelle valeur dans la config pour la prochaine exécution
        config["pcb2gcode_exe"] = pcb2gcode_exe
        sauvegarder_config(config)

    return pcb2gcode_exe


def executer_commande_pcb2gcode(
    commande: list[str],
) -> subprocess.CompletedProcess[str]:
    """Lance pcb2gcode avec la commande générée."""
    return subprocess.run(commande, capture_output=False, text=True, check=False)


def finaliser_generation(
    resultat: subprocess.CompletedProcess[str], output: Path
) -> None:
    """Applique le post-traitement après l'exécution de pcb2gcode."""
    if resultat.returncode == 0:
        print("\n[SUCCÈS] Fichiers G-code générés dans :", output)
        nettoyer_gcode_grbl(output)
    else:
        print(f"\n[ERREUR] pcb2gcode a retourné le code {resultat.returncode}")


def lancer(
    config: dict[str, Any],
    dossier: str | Path,
    offset_x_mm: float = 0.0,
    offset_y_mm: float = 0.0,
) -> None:
    """
    Lance le processus de génération de G-code
    à partir des fichiers GBR en utilisant pcb2gcode.
    """

    dossier_source = Path(dossier)
    front, back, drill, output, hauteur_pcb, largeur_pcb = lecture_chemin_dossier(
        dossier_source
    )
    front_effectif, back_effectif, drill_effectif = verifier_fichiers_entree(
        front, back, drill
    )

    if afficher_aucune_entree_si_besoin(front_effectif, back_effectif, drill_effectif):
        return

    back_effectif = filtrer_pistes_back(back_effectif)
    drill_effectif = filtrer_percages_drill(drill_effectif)

    output = preparer_dossier_sortie(output)
    pcb2gcode_exe = resoudre_executable_pcb2gcode(config)

    front_effectif, back_effectif, drill_effectif = preparer_gerbers_avec_offset(
        dossier_source,
        front_effectif,
        back_effectif,
        drill_effectif,
        output,
        offset_x_mm,
        offset_y_mm,
    )

    commande = construire_commande(
        config,
        front_effectif,
        back_effectif,
        drill_effectif,
        output,
    )

    print("\nLancement de pcb2gcode...")
    print(f"Output : {output}\n")
    print("-" * 40)

    try:
        resultat = executer_commande_pcb2gcode(commande)
        output_cut_file = output / "cuts.ngc"
        generer_gcode_cuts_dimensions(largeur_pcb, hauteur_pcb, output_cut_file)
        print("-" * 40)
        finaliser_generation(resultat, output)
    except FileNotFoundError:
        print(f"[ERREUR] pcb2gcode.exe introuvable : {pcb2gcode_exe}")


def generer_gcode_cuts_dimensions(
    largeur: float,
    longueur: float,
    chemin_sortie: str | Path,
    margin: float = 2.0,
    passes: list[float] | None = None,
    feed: int = 300,
    safe_z: float = 5.0,
) -> bool:
    """Génère un fichier G-code de 'cuts' rectangulaires autour d'un PCB de dimensions données (mm)."""
    if passes is None:
        passes = [-0.4, -0.8, -2.0]

    x0 = round(-margin, 4)
    y0 = round(-margin, 4)
    x1 = round(largeur + margin, 4)
    y1 = round(longueur + margin, 4)

    lignes = construire_lignes_cuts(x0, y0, x1, y1, passes, feed, safe_z)

    try:
        with open(chemin_sortie, "w", encoding="utf-8") as f:
            f.writelines(lignes)
        print(f"[OK] Fichier cuts généré: {chemin_sortie}")
        return True
    except OSError as e:
        print(f"Erreur écriture {chemin_sortie}: {e}")
        return False


def construire_lignes_cuts(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    passes: list[float],
    feed: int,
    safe_z: float,
) -> list[str]:
    """Construit les lignes G-code pour la découpe rectangulaire."""
    lignes: list[str] = []
    lignes.append("G21\t\t\t; mm\n")
    lignes.append("G90\t\t\t; absolu\n")
    lignes.append(f"G00 X{x0} Y{y0} Z{safe_z}\n")
    lignes.append("; --- Passe 1 ---\t; Optionnelle\n")

    for i, profondeur in enumerate(passes):
        lignes.extend(construire_lignes_passe(i, profondeur, x1, y1, x0, y0, feed))

    lignes.append(f"G00 Z{safe_z}\n")
    lignes.append("G00 X0 Y0\n")
    return lignes


def construire_lignes_passe(
    index_passe: int,
    profondeur: float,
    x1: float,
    y1: float,
    x0: float,
    y0: float,
    feed: int,
) -> list[str]:
    """Construit les lignes G-code d'une passe de découpe."""
    lignes: list[str] = []
    if index_passe == 0:
        lignes.append(f"G01 Z{profondeur} F{feed} \t; F = vitesse en mm/s\n")
    else:
        lignes.append(f"; --- Passe {index_passe + 1} ---\n")
        lignes.append(f"G01 Z{profondeur}\n")
    lignes.append(f"G01 X{x1}\n")
    lignes.append(f"G01 Y{y1}\n")
    lignes.append(f"G01 X{x0}\n")
    lignes.append(f"G01 Y{y0}\n")
    return lignes


def main() -> None:
    """Point d'entrée du programme, affiche le menu et gère les choix de l'utilisateur."""
    config = charger_config()

    while True:
        afficher_menu_principal()
        choix = lire_choix_menu_principal()
        if gerer_choix_menu(choix, config):
            break


def afficher_menu_principal() -> None:
    """Affiche le menu principal."""
    print("=== Lanceur pcb2gcode ===")
    print("1. Lancer pcb2gcode")
    print("2. Modifier les paramètres")
    print("3. Afficher les paramètres")
    print("4. Lancer pcb2gcode sur plusieurs projets")
    print("5. Quitter")


def lire_choix_menu_principal() -> str:
    """Lit le choix du menu principal."""
    return input("> ").strip()


def demander_chemin(chemin_cherche: str) -> str:
    """Demande le dossier contenant les fichiers Gerber d'un projet."""
    return input_retour(
        f"Chemin du dossier contenant {chemin_cherche} (ou 'retour' pour revenir):"
    )


def gerer_choix_menu(choix: str, config: dict[str, Any]) -> bool:
    """Exécute l'action associée au choix du menu principal."""
    try:
        if choix == "1":
            os.system("cls")
            demande_user = demander_chemin("les fichiers GBR")
            offset_x_mm, offset_y_mm = demander_offset_gerber()
            lancer(config, demande_user, offset_x_mm, offset_y_mm)
            return False
        if choix == "2":
            os.system("cls")
            modifier_parametres(config)
            return False
        if choix == "3":
            os.system("cls")
            afficher_parametres(config)
            return False
        if choix == "4":
            os.system("cls")
            demande_user = demander_chemin("les dossiers des projets")
            offset_x_mm, offset_y_mm = demander_offset_gerber()
            lecture_dossier_de_dossier(config, demande_user, offset_x_mm, offset_y_mm)
            return False
        if choix == "5":
            os.system("cls")
            return True
        print("Choix invalide.\n")
        return False
    except RetourMenu:
        os.system("cls")
        print("Retour au menu principal demandé — annulation de l'opération.")
        return False


if __name__ == "__main__":
    main()
