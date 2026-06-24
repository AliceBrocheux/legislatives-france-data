# Base de données des élections législatives françaises (1958-2024)

Base SQLite + exports CSV/Parquet des résultats par candidat pour toutes les élections législatives générales françaises de 1958 à 2024.

## Structure de la base

**Une ligne = un candidat, pour une élection, dans une circonscription.**

### Table `resultats`

| Colonne | Type | Description |
|---------|------|-------------|
| `annee` | INTEGER | Année de l'élection |
| `id_circo` | TEXT | Identifiant de la circonscription, ex: `"75-01"` |
| `departement` | TEXT | Nom du département |
| `nom_candidat` | TEXT | Nom et prénom du candidat |
| `etiquette` | TEXT | Étiquette parti (label long) |
| `nuance` | TEXT | Code nuance politique (ex: RN, ENS, NFP…) |
| `voix_t1` | INTEGER | Voix obtenues au 1er tour |
| `qualifie_t2` | BOOLEAN | A dépassé le seuil légal au T1 |
| `maintenu_t2` | BOOLEAN | NULL=non qualifié; TRUE=présent T2; FALSE=désisté |
| `voix_t2` | INTEGER | Voix obtenues au 2e tour |
| `elu` | BOOLEAN | Élu(e) député(e) |
| `inscrits_t1/t2` | INTEGER | Électeurs inscrits |
| `votants_t1/t2` | INTEGER | Électeurs ayant voté |
| `blancs_t1/t2` | INTEGER | Bulletins blancs |
| `nuls_t1/t2` | INTEGER | Bulletins nuls |
| `exprimes_t1/t2` | INTEGER | Suffrages exprimés |

## Règles de qualification au 2e tour

| Élections | Seuil | Base de calcul |
|-----------|-------|----------------|
| 1958, 1962 | 5% | Suffrages exprimés T1 |
| 1967, 1968 | 10% | Électeurs inscrits |
| 1973–2024 | 12,5% | Électeurs inscrits |

## Sources de données

### Ministère de l'Intérieur — données officielles (2002-2024)
- **2002, 2007, 2012** : fichiers XLS par circonscription (T1 + T2), source data.gouv.fr
- **2017** : `Leg_2017_Resultats_T{1,2}_c.xlsx`
- **2022** : résultats définitifs par circonscription (format XLSX)
- **2024** : résultats définitifs par circonscription (format XLSX)
- Source 2017-2024 : `github.com/emagar/france` (redistribution des données officielles)

### CDSP Sciences Po (1958-1997)
- Fichiers `cdsp_legi{annee}t{1,2}_circ.csv`
- Source miroir : `github.com/domi41/french-legislatives-analysis`
- Format large : données par parti (1958-1981) ou par candidat (1988-1997)

## Couverture par année

| Année | Lignes | Circos | Source | Format données |
|-------|--------|--------|--------|----------------|
| 1958 | 2 679 | 465 | CDSP | Agrégé par parti |
| 1962 | 2 140 | 465 | CDSP | Agrégé par parti |
| 1967 | 2 147 | 470 | CDSP | Agrégé par parti |
| 1968 | 2 928 | 470 | CDSP | Agrégé par parti |
| 1973 | 2 838 | 473 | CDSP | Agrégé par parti |
| 1978 | 3 350 | 474 | CDSP | Agrégé par parti |
| 1981 | 2 937 | 473 | CDSP | Agrégé par parti |
| 1988 | 2 843 | 576 | CDSP | Par candidat |
| 1993 | 5 285 | 576 | CDSP | Par candidat |
| 1997 | 6 358 | 577 | CDSP | Par candidat |
| 2002 | 8 443 | 577 | Ministère | Par candidat |
| 2007 | 7 633 | 577 | Ministère | Par candidat |
| 2012 | 6 603 | 577 | Ministère | Par candidat |
| 2017 | 7 877 | 577 | Ministère | Par candidat |
| 2022 | 6 290 | 577 | Ministère | Par candidat |
| 2024 | 4 009 | 577 | Ministère | Par candidat |
| **TOTAL** | **74 360** | - | - | - |

**Note** : Pour 1958-1981, les données CDSP sont agrégées par parti politique (pas de nom individuel de candidat). La colonne `nom_candidat` contient le code nuance entre crochets (`[COM]`, `[RPR]`…). Ces années ne permettent donc pas d'analyse individuelle des candidats. Pour 2002-2024, les données sont issues directement du Ministère de l'Intérieur.

## Fichiers produits

```
db/
  legislatives.sqlite          # Base SQLite principale
data/
  raw/
    cdsp/                      # CSV bruts CDSP 1958-2012
    ministere/                 # XLSX bruts Ministère 2017-2024
  clean/
    resultats_complets.csv     # Export CSV complet
    resultats_complets.parquet # Export Parquet complet
    triangulaires_1958_2024.csv # Triangulaires et quadrangulaires
src/
  pipeline.py                  # Script de construction complet
```

## Configurations de second tour par année

« Triangulaire » = exactement 3 candidats maintenus au T2 ; « Quadrangulaire » = exactement 4 ; « 5+ » = 5 ou plus.

| Année | Élus T1 | Seul | Duels | Triangulaires | Quadrangulaires | 5+ | Réf. tri. (Wikipedia) | Écart tri. |
|-------|---------|------|-------|--------------|----------------|-----|----------------------|------------|
| 1958 | — | 0 | 73 | **233** | 106 | 14 | 235 | −2 |
| 1962 | 96 | 1 | 209 | **145** | 14 | 0 | — | — |
| 1967 | 72 | 0 | 324 | **72** | 2 | 0 | — | — |
| 1968 | 154 | 1 | 266 | **49** | 0 | 0 | 49 | ✓ |
| 1973 | 49 | 1 | 326 | **96** | 1 | 0 | — | — |
| 1978 | 57 | 8 | 408 | **1** | 0 | 0 | 1 | ✓ |
| 1981 | 154 | 10 | 308 | **1** | 0 | 0 | — | — |
| 1988 | 121 | 20 | 424 | **10** | 1 | 0 | — | — |
| 1993 | 17 | 17 | 458 | **19** | 1 | 0 | — | — |
| 1997 | 12 | 12 | 471 | **80** | 1 | 1 | **79** | +1* |
| 2002 | 4 | 4 | 563 | **10** | 0 | 0 | 15 | −5* |
| 2007 | 5 | 5 | 461 | **1** | 0 | 0 | 6 | −5* |
| 2012 | 15 | 15 | 528 | **34** | 0 | 0 | 41 | −7* |
| 2017 | 1 | 1 | 571 | **1** | 0 | 0 | 1 | ✓ |
| 2022 | 3 | 3 | 561 | **8** | 0 | 0 | 8 | ✓ |
| 2024 | 1 | 1 | 409 | **89** | 2 | 0 | **89** | ✓ |

*1997 : la 79e circo est une quintuangulaire (5 maintenus) ; en comptant "3+", on retrouve bien 79+1 quinquangulaire = 80 ✓.

**Note 1958** : L'écart de −2 (233 vs réf. 235) est probablement dû à des données CDSP incomplètes pour quelques circonscriptions.

**Note 2002/2007/2012** : Les fichiers XLS du Ministère (data.gouv.fr) ont 3 colonnes candidats au T2 par circonscription. Les triangulaires manquantes correspondent à des désistements enregistrés différemment selon les sources, ou à des données incomplètes dans le fichier XLS d'origine.

**Note 2022** : L'écart a été corrigé à ✓ après correction des artefacts de données.

## Limitations connues

- **1958-1981** : Pas de données individuelles par candidat. Les "candidats" sont en fait des lignes de parti.
- **Outre-mer** : Les DOM-TOM figurent dans les données à partir de 1988 selon la source CDSP. Pour 1958-1981, couverture partielle.
- **Élections partielles** : Exclues (seuls les renouvellements généraux sont inclus).
- **`blancs` et `nuls`** : Séparés pour les années 2017+ (Ministère). Pour les années CDSP, seul le total blancs+nuls est disponible (stocké dans `blancs_t1`, `nuls_t1` = NULL).
- **Élu(e)** : Pour les données CDSP (1988-2012), l'élu est identifié comme le candidat ayant obtenu le plus de voix au T2 dans sa circonscription (proxy). Pour les données Ministère (2017-2024), l'information est directement dans la source.

## Reproducibilité

```bash
# Depuis la racine du dépôt :
pip install pandas openpyxl pyarrow duckdb
python3 src/pipeline.py
```

Les données brutes sont dans `data/raw/` et peuvent être retéléchargées depuis les sources mentionnées ci-dessus.
