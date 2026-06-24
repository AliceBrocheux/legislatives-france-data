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

### Ministère de l'Intérieur via emagar/france (2017-2024)
- **2017** : `Leg_2017_Resultats_T{1,2}_c.xlsx`
- **2022** : résultats définitifs par circonscription (format XLSX)
- **2024** : résultats définitifs par circonscription (format XLSX)
- Source GitHub : `github.com/emagar/france` (redistribution des données officielles du Ministère)

### CDSP Sciences Po (1958-2012)
- Fichiers `cdsp_legi{annee}t{1,2}_circ.csv`
- Source miroir : `github.com/domi41/french-legislatives-analysis`
- Format large : données par parti (1958-1981) ou par candidat (1988-2012)

## Couverture par année

| Année | Lignes | Circos | Source | Format données |
|-------|--------|--------|--------|----------------|
| 1958 | 2 679 | 465 | CDSP | Agrégé par parti |
| 1962 | 2 140 | 465 | CDSP | Agrégé par parti |
| 1967 | 2 147 | 470 | CDSP | Agrégé par parti |
| 1968 | 2 248 | 470 | CDSP | Agrégé par parti |
| 1973 | 2 838 | 473 | CDSP | Agrégé par parti |
| 1978 | 3 350 | 474 | CDSP | Agrégé par parti |
| 1981 | 4 732 | 473 | CDSP | Agrégé par parti |
| 1988 | 2 843 | 576 | CDSP | Par candidat |
| 1993 | 5 285 | 576 | CDSP | Par candidat |
| 1997 | 6 358 | 577 | CDSP | Par candidat |
| 2002 | 8 443 | 577 | CDSP | Par candidat |
| 2007 | 7 634 | 577 | CDSP | Par candidat |
| 2012 | 6 610 | 577 | CDSP | Par candidat |
| 2017 | 7 877 | 577 | Ministère | Par candidat |
| 2022 | 6 290 | 577 | Ministère | Par candidat |
| 2024 | 4 009 | 577 | Ministère | Par candidat |
| **TOTAL** | **75 483** | - | - | - |

**Note** : Pour 1958-1981, les données CDSP sont agrégées par parti politique (pas de nom individuel de candidat). La colonne `nom_candidat` contient le code nuance entre crochets (`[COM]`, `[RPR]`…). Ces années ne permettent donc pas d'analyse individuelle des candidats.

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

## Triangulaires par année

Circos avec 3+ candidats présents au 2e tour (maintenus ou non désistés) :

| Année | Triangulaires | Référence | Écart |
|-------|--------------|-----------|-------|
| 1958 | 353 | - | - |
| 1962 | 159 | - | - |
| 1967 | 74 | - | - |
| 1973 | 49 | - | - |
| 1978 | 1 | - | - |
| 1981 | 7 | - | - |
| 1988 | 9 | - | - |
| 1993 | 15 | - | - |
| 1997 | 79 | **79** | ✓ exact |
| 2002 | 10 | - | - |
| 2007 | 1 | - | - |
| 2012 | 33 | - | - |
| 2017 | 1 | - | - |
| 2022 | 7 | 8 | -1 |
| 2024 | 91 | ~89 | +2 |

**Note 2022** : L'écart de 1 est dû à au moins une circo où un candidat juste en dessous du seuil de 12,5% des inscrits a participé au T2 via désistement entrant (remplaçant un candidat désisté). Ces cas ne sont pas captés par les règles de qualification strictes.

**Note 2024** : Le chiffre de référence (89) est celui des triangulaires effective après désistements massifs. Notre compte de 91 recense les circos où 3+ candidats ont effectivement voté au T2 d'après les résultats officiels. L'écart de 2 peut s'expliquer par des circos où le décompte officiel intègre des situations particulières (Outre-mer, etc.).

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
