# Sky-Nav météo

Chaîne météo de l'appli Sky-Nav (The Maracuja Company).

Toutes les 3 heures, une tâche GitHub télécharge le dernier réseau du modèle
**AROME de Météo-France** (0,025°), le convertit en petites images WebP
(vent au sol et de 100 à 3 000 m, rafales, pluie, nuages, température,
CAPE, base des nuages) et les dépose sur Cloudflare R2, où l'appli les lit.

- `build_meteo.py` : GRIB2 → tuiles WebP + `manifest.json`
- `upload_r2.py` : envoi sur R2, puis `arome/latest.json`, garde les 2 derniers réseaux
- `.github/workflows/meteo.yml` : la tâche planifiée

Secrets du dépôt : `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`.

Source des données : Météo-France, modèle AROME, sous Licence Ouverte 2.0
(Etalab). Données publiées sur https://meteo.data.gouv.fr.
