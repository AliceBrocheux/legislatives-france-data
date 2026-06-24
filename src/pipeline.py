#!/usr/bin/env python3
"""
Pipeline de construction de la base des élections législatives françaises (1958-2024).

Sources:
  - CDSP Sciences Po (1958-2012) : format CSV large, candidats en colonnes
    Source: github.com/domi41/french-legislatives-analysis
  - Ministère de l'Intérieur (2017-2024) : format XLSX large, candidats en colonnes
    Source: github.com/emagar/france

Architecture:
  - Une ligne = un candidat, une élection, une circonscription
  - Table unique `resultats` dans db/legislatives.sqlite
  - Exports CSV et Parquet dans data/clean/
"""

import os
import re
import sqlite3
import warnings
import pandas as pd
import numpy as np
from pathlib import Path

warnings.filterwarnings('ignore')

ROOT = Path('/home/user/legislatives-france-data')
RAW_CDSP = ROOT / 'data' / 'raw' / 'cdsp'
RAW_MIN = ROOT / 'data' / 'raw' / 'ministere'
CLEAN_DIR = ROOT / 'data' / 'clean'
DB_PATH = ROOT / 'db' / 'legislatives.sqlite'

# ─── Règles de qualification au T2 ───────────────────────────────────────────

def seuil_qualification(annee: int):
    """
    Retourne (pct, base) selon les règles légales.
    base est 'exprimes' ou 'inscrits'.
    """
    if annee in (1958, 1962):
        return 0.05, 'exprimes'
    elif annee in (1967, 1968):
        return 0.10, 'inscrits'
    else:
        return 0.125, 'inscrits'


def calculer_qualifie(voix_t1, inscrits_t1, exprimes_t1, annee):
    """Calcule si un candidat est qualifié pour le T2."""
    pct, base = seuil_qualification(annee)
    if pd.isna(voix_t1):
        return False
    ref = exprimes_t1 if base == 'exprimes' else inscrits_t1
    if pd.isna(ref) or ref == 0:
        return False
    return bool(voix_t1 >= pct * ref)


# ─── Parsers CDSP (1958-2012) ─────────────────────────────────────────────────

CDSP_EARLY_YEARS = {1958, 1962, 1967, 1968}  # données agrégées par parti

def detect_cdsp_format(df_raw):
    """Détecte si les données CDSP sont par parti (avant 1988) ou par candidat (1988+)."""
    cols = list(df_raw.columns)
    # Format candidat: contient "Nom candidat" ou "Nom 1" etc.
    has_candidate = any('Nom' in str(c) for c in cols)
    return 'candidat' if has_candidate else 'parti'


def parse_cdsp_candidats(df_t1_raw, df_t2_raw, annee):
    """
    Parse les fichiers CDSP format candidat (1988-2012).
    Format: Code dept, dept, circo, Inscrits, Votants, Exprimés, Blancs et nuls,
            [Taux participation], 1 Sexe candidat, 1 Nom candidat, 1 Prénom candidat,
            1 Etiquette liste, 1 nuance, 1 voix, 1 Accès second tour, ...
    """
    def parse_round(df_raw, is_t2=False):
        """Convertit format large -> long pour un tour."""
        cols = list(df_raw.columns)
        # Identifier les colonnes de base
        base_cols = ['dept_code', 'departement', 'circo']
        meta_cols_map = {}

        # Trouver les colonnes d'en-tête
        for i, c in enumerate(cols):
            c_low = str(c).lower()
            if 'inscrits' in c_low:
                meta_cols_map['inscrits'] = i
            elif 'votants' in c_low:
                meta_cols_map['votants'] = i
            elif 'exprim' in c_low:
                meta_cols_map['exprimes'] = i
            elif 'blancs' in c_low and 'nuls' in c_low:
                meta_cols_map['blancs_nuls'] = i
            elif 'blancs' in c_low and 'nuls' not in c_low:
                meta_cols_map['blancs'] = i
            elif 'nuls' in c_low and 'blancs' not in c_low:
                meta_cols_map['nuls'] = i

        # Trouver les blocs candidats
        # Pattern: "N Sexe candidat" ou "N Nom candidat" ou "N voix"
        # Détecter le nombre max de candidats
        cand_nums = set()
        for c in cols:
            m = re.match(r'^(\d+)\s+(?:Sexe|Nom|Prénom|Etiquette|nuance|voix|Accès)', str(c))
            if m:
                cand_nums.add(int(m.group(1)))

        if not cand_nums:
            return pd.DataFrame()

        max_cand = max(cand_nums)
        rows = []

        for _, row in df_raw.iterrows():
            dept_code = str(row.iloc[0]).zfill(2)
            departement = str(row.iloc[1])
            circo = str(row.iloc[2]).zfill(2)
            id_circo = f"{dept_code}-{circo}"

            # Meta
            inscrits = _to_int(row.iloc[meta_cols_map.get('inscrits', 3)])
            votants = _to_int(row.iloc[meta_cols_map.get('votants', 4)])
            exprimes = _to_int(row.iloc[meta_cols_map.get('exprimes', 5)])
            blancs_nuls = _to_int(row.iloc[meta_cols_map.get('blancs_nuls', 6)])
            blancs = _to_int(row.iloc[meta_cols_map.get('blancs', -1)]) if 'blancs' in meta_cols_map else None
            nuls = _to_int(row.iloc[meta_cols_map.get('nuls', -1)]) if 'nuls' in meta_cols_map else None

            # Si blancs/nuls séparés non disponibles, blancs_nuls = total
            if blancs is None and nuls is None:
                blancs = blancs_nuls  # approximation
                nuls = None

            for n in range(1, max_cand + 1):
                prefix = f"{n} "
                nom_col = next((c for c in cols if str(c) == f"{n} Nom candidat"), None)
                if nom_col is None:
                    continue

                nom = str(row[nom_col]) if nom_col in cols and str(row[nom_col]) != 'nan' else None
                if nom is None or nom == 'nan' or nom == '':
                    continue

                prenom_col = next((c for c in cols if str(c) == f"{n} Prénom candidat"), None)
                etiq_col = next((c for c in cols if str(c) == f"{n} Etiquette liste"), None)
                nuance_col = next((c for c in cols if str(c) == f"{n} nuance"), None)
                voix_col = next((c for c in cols if str(c) == f"{n} voix"), None)
                acces_col = next((c for c in cols if str(c) == f"{n} Accès second tour"), None)

                prenom = str(row[prenom_col]) if prenom_col else ''
                etiquette = str(row[etiq_col]) if etiq_col else ''
                nuance = str(row[nuance_col]) if nuance_col else ''
                voix = _to_int(row[voix_col]) if voix_col else None
                acces = str(row[acces_col]).strip().upper() if acces_col else ''

                nom_complet = f"{nom} {prenom}".strip()

                cand_row = {
                    'id_circo': id_circo,
                    'departement': departement,
                    'dept_code': dept_code,
                    'circo': circo,
                    'nom_candidat': nom_complet,
                    'etiquette': etiquette if etiquette != 'nan' else None,
                    'nuance': nuance if nuance != 'nan' else None,
                    'inscrits': inscrits,
                    'votants': votants,
                    'exprimes': exprimes,
                    'blancs': blancs,
                    'nuls': nuls,
                    'voix': voix,
                    'acces': acces,
                }
                rows.append(cand_row)

        return pd.DataFrame(rows)

    df_t1 = parse_round(df_t1_raw, is_t2=False)
    df_t2 = parse_round(df_t2_raw, is_t2=True) if df_t2_raw is not None else pd.DataFrame()

    return merge_tours(df_t1, df_t2, annee)


def parse_cdsp_partis(df_t1_raw, df_t2_raw, annee):
    """
    Parse les fichiers CDSP format parti (1958-1968).
    Données agrégées par parti : pas de nom de candidat.
    On génère une ligne par parti présent.
    """
    def parse_round(df_raw, is_t2=False):
        cols = list(df_raw.columns)
        # Row 0 = labels longs, Row 1 = codes nuances (ligne 2 du CSV)
        # Les données réelles commencent à la ligne appropriée
        # Le df passé a déjà les 2 premières lignes comme header

        # La 2e ligne contient les codes nuances
        nuance_codes = list(df_raw.iloc[0])  # codes nuances

        party_cols = cols[7:]  # après les 7 premières colonnes de meta
        party_codes = nuance_codes[7:]

        rows = []
        for _, row in df_raw.iloc[1:].iterrows():
            dept_code = str(row.iloc[0]).zfill(2)
            departement = str(row.iloc[1])
            circo = str(row.iloc[2]).zfill(2)
            id_circo = f"{dept_code}-{circo}"

            inscrits = _to_int(row.iloc[3])
            votants = _to_int(row.iloc[4])
            exprimes = _to_int(row.iloc[5])
            blancs_nuls = _to_int(row.iloc[6])

            for i, (pcol, pcode) in enumerate(zip(party_cols, party_codes)):
                voix = _to_int(row[pcol])
                if voix is None or voix == 0:
                    continue

                rows.append({
                    'id_circo': id_circo,
                    'departement': departement,
                    'dept_code': dept_code,
                    'circo': circo,
                    'nom_candidat': f"[{pcol}]",  # pas de nom individuel
                    'etiquette': pcol,
                    'nuance': str(pcode) if str(pcode) != 'nan' else pcol[:10],
                    'inscrits': inscrits,
                    'votants': votants,
                    'exprimes': exprimes,
                    'blancs': blancs_nuls,
                    'nuls': None,
                    'voix': voix,
                    'acces': '',
                })

        return pd.DataFrame(rows)

    df_t1 = parse_round(df_t1_raw, is_t2=False)
    df_t2 = parse_round(df_t2_raw, is_t2=True) if df_t2_raw is not None else pd.DataFrame()

    return merge_tours(df_t1, df_t2, annee)


def merge_tours(df_t1, df_t2, annee):
    """Fusionne T1 et T2 sur id_circo + nom_candidat."""
    if df_t1.empty:
        return pd.DataFrame()

    pct, base = seuil_qualification(annee)

    # Calculer qualifie_t2
    df_t1['qualifie_t2'] = df_t1.apply(
        lambda r: calculer_qualifie(
            r['voix'], r['inscrits'], r['exprimes'], annee
        ), axis=1
    )

    if df_t2.empty:
        df_t1['voix_t2'] = None
        df_t1['inscrits_t2'] = None
        df_t1['votants_t2'] = None
        df_t1['blancs_t2'] = None
        df_t1['nuls_t2'] = None
        df_t1['exprimes_t2'] = None
        df_t1['maintenu_t2'] = None
        df_t1['elu'] = False
        return _rename_t1(df_t1)

    # Index T2 par id_circo + nom_candidat (normalisé)
    df_t2_idx = df_t2.rename(columns={
        'voix': 'voix_t2',
        'inscrits': 'inscrits_t2',
        'votants': 'votants_t2',
        'exprimes': 'exprimes_t2',
        'blancs': 'blancs_t2',
        'nuls': 'nuls_t2',
    }).set_index(['id_circo', 'nom_candidat'])

    df_t2_idx = df_t2_idx[['voix_t2', 'inscrits_t2', 'votants_t2',
                             'exprimes_t2', 'blancs_t2', 'nuls_t2',
                             'acces']].copy()

    # Merge
    df = df_t1.set_index(['id_circo', 'nom_candidat'])
    df = df.join(df_t2_idx, how='left', rsuffix='_t2')
    df = df.reset_index()

    # maintenu_t2
    def calc_maintenu(row):
        if not row['qualifie_t2']:
            return None  # NULL
        if pd.notna(row.get('voix_t2')) and row.get('voix_t2', 0) > 0:
            return True
        return False

    df['maintenu_t2'] = df.apply(calc_maintenu, axis=1)

    # elu: présent au T2 et acces_t2 indique l'élu
    # Dans les données CDSP, on n'a pas toujours l'info "élu"
    # On l'identifie comme: candidat au T2 avec le plus de voix dans sa circo
    if 'voix_t2' in df.columns:
        df['voix_t2_num'] = pd.to_numeric(df['voix_t2'], errors='coerce')
        df['elu'] = False
        mask = df['voix_t2_num'].notna()
        if mask.any():
            idx_max = df[mask].groupby('id_circo')['voix_t2_num'].idxmax()
            df.loc[idx_max, 'elu'] = True
        df = df.drop(columns=['voix_t2_num'])
    else:
        df['elu'] = False

    return _rename_t1(df)


def _rename_t1(df):
    """Renomme les colonnes T1."""
    rename_map = {
        'voix': 'voix_t1',
        'inscrits': 'inscrits_t1',
        'votants': 'votants_t1',
        'exprimes': 'exprimes_t1',
        'blancs': 'blancs_t1',
        'nuls': 'nuls_t1',
    }
    df = df.rename(columns=rename_map)
    return df


# ─── Parsers Ministère (2017-2024) ───────────────────────────────────────────

def detect_xlsx_format(df_raw):
    """Détecte le format des xlsx Ministère."""
    cols = list(df_raw.iloc[0])
    if 'Code département' in cols or 'Code circonscription législative' in cols:
        return '2024'
    elif 'depn' in cols or 'département' in str(cols[1]).lower():
        return '2017_2022'
    return 'unknown'


def parse_xlsx_ministere(path_t1, path_t2, annee):
    """Parse les fichiers XLSX du Ministère (2017, 2022, 2024)."""
    df_t1_raw = pd.read_excel(path_t1, header=None)
    df_t2_raw = pd.read_excel(path_t2, header=None)

    fmt = detect_xlsx_format(df_t1_raw)

    if fmt == '2024':
        return parse_xlsx_2024(df_t1_raw, df_t2_raw, annee)
    else:
        return parse_xlsx_2017_2022(df_t1_raw, df_t2_raw, annee)


def parse_xlsx_2024(df_t1_raw, df_t2_raw, annee):
    """Parse format 2024: header row 0, data from row 2 (row 1 est vide)."""

    def parse_round(df_raw, is_t2=False):
        # Row 0 = headers
        headers = list(df_raw.iloc[0])
        # Data starts at row 1 (2024 T1) ou row 1 (2024 T2)
        # Détecter si row 1 est vide
        first_data_row = 1
        if all(pd.isna(v) or str(v) == 'nan' for v in df_raw.iloc[1]):
            first_data_row = 2

        df = df_raw.iloc[first_data_row:].copy()
        df.columns = headers
        df = df.reset_index(drop=True)

        # Identifier colonnes meta
        col_dept = next(c for c in headers if 'département' in str(c).lower() or 'departement' in str(c).lower())
        col_circo = next(c for c in headers if 'circo' in str(c).lower() or 'circonscription' in str(c).lower())

        # Détecter colonnes inscrits/votants
        inscrits_col = next((c for c in headers if str(c).lower() == 'inscrits'), None)
        votants_col = next((c for c in headers if str(c).lower() == 'votants'), None)
        exprimes_col = next((c for c in headers if 'exprim' in str(c).lower()), None)
        blancs_col = next((c for c in headers if str(c).lower() == 'blancs'), None)
        nuls_col = next((c for c in headers if str(c).lower() == 'nuls'), None)

        # Trouver numéros de candidats
        cand_nums = set()
        for c in headers:
            m = re.search(r'(\d+)$', str(c))
            if m and any(kw in str(c) for kw in ['Nuance', 'Nom', 'Voix', 'Elu']):
                cand_nums.add(int(m.group(1)))

        rows = []
        for _, row in df.iterrows():
            dept_raw = str(row[col_dept])
            circo_raw = str(row[col_circo])

            # Normaliser dept_code
            dept_code = dept_raw.zfill(2) if dept_raw.isdigit() else dept_raw

            # Normaliser circo: les codes circo 2024 sont comme "101", "102" = dept 01 circo 01
            # ou "0101" format 4 chiffres
            if circo_raw.isdigit() and len(circo_raw) >= 3:
                # Format 3 ou 4 chiffres: premiers = dept, derniers 2 = circo
                circo_num = circo_raw[-2:]
                dept_code_from_circo = circo_raw[:-2].zfill(2)
                dept_code = dept_code_from_circo
            else:
                circo_num = circo_raw.zfill(2)

            id_circo = f"{dept_code}-{circo_num}"

            inscrits = _to_int(row.get(inscrits_col))
            votants = _to_int(row.get(votants_col))
            exprimes = _to_int(row.get(exprimes_col))
            blancs = _to_int(row.get(blancs_col))
            nuls = _to_int(row.get(nuls_col))

            # Trouver la colonne département libellé
            dept_label = ''
            for c in headers:
                if 'libellé' in str(c).lower() and 'département' in str(c).lower():
                    dept_label = str(row[c])
                    break
            if not dept_label:
                dept_label = dept_raw

            for n in sorted(cand_nums):
                # Trouver colonnes pour candidat n
                nuance = None
                nom = None
                prenom = None
                voix = None
                elu_raw = None

                for c in headers:
                    c_str = str(c)
                    if c_str.endswith(f' {n}') or c_str.endswith(f'{n}'):
                        last_num = re.search(r'(\d+)$', c_str)
                        if last_num and int(last_num.group(1)) == n:
                            c_low = c_str.lower()
                            if 'nuance' in c_low:
                                nuance = str(row[c]) if str(row[c]) != 'nan' else None
                            elif 'prénom' in c_low or 'prenom' in c_low:
                                prenom = str(row[c]) if str(row[c]) != 'nan' else None
                            elif 'nom' in c_low:
                                nom = str(row[c]) if str(row[c]) != 'nan' else None
                            elif 'voix' in c_low:
                                voix = _to_int(row[c])
                            elif 'elu' in c_low or 'élu' in c_low:
                                elu_raw = row[c]

                if nom is None:
                    continue

                nom_complet = f"{nom} {prenom}".strip() if prenom else nom

                # Dans 2024, Elu = position numéro si élu, sinon le numéro de panneau
                # En T1: "2" = qualifié (accès T2), en T2: "élu" = élu
                elu = False
                if is_t2 and elu_raw is not None:
                    elu_str = str(elu_raw).lower()
                    if 'élu' in elu_str or 'elu' in elu_str:
                        elu = True

                rows.append({
                    'id_circo': id_circo,
                    'departement': dept_label,
                    'dept_code': dept_code,
                    'circo': circo_num,
                    'nom_candidat': nom_complet,
                    'etiquette': nuance,
                    'nuance': nuance,
                    'inscrits': inscrits,
                    'votants': votants,
                    'exprimes': exprimes,
                    'blancs': blancs,
                    'nuls': nuls,
                    'voix': voix,
                    'acces': '',
                    'elu': elu,
                })

        return pd.DataFrame(rows)

    df_t1 = parse_round(df_t1_raw, is_t2=False)
    df_t2 = parse_round(df_t2_raw, is_t2=True)

    return merge_tours_ministere(df_t1, df_t2, annee, has_elu_in_t2=True)


def parse_xlsx_2017_2022(df_t1_raw, df_t2_raw, annee):
    """Parse format 2017/2022: header row 0."""

    def parse_round(df_raw, is_t2=False):
        headers = list(df_raw.iloc[0])
        df = df_raw.iloc[1:].copy()
        df.columns = headers
        df = df.reset_index(drop=True)

        # Colonnes meta
        dept_col = headers[0]  # 'depn'
        dept_label_col = headers[1]  # 'département'
        circo_col = headers[2]  # 'circonscription'

        # Inscrits, votants, exprimés
        inscrits_col = next((c for c in headers if str(c).lower() == 'inscrits'), None)
        votants_col = next((c for c in headers if str(c).lower() == 'votants'), None)
        exprimes_col = next((c for c in headers if 'exprim' in str(c).lower()), None)
        abstentions_col = next((c for c in headers if 'abstention' in str(c).lower()), None)
        blancs_col = next((c for c in headers if str(c).lower() == 'blancs'), None)
        nuls_col = next((c for c in headers if str(c).lower() == 'nuls'), None)

        # Numéros de candidats
        cand_nums = set()
        for c in headers:
            m = re.search(r'(\d+)$', str(c))
            if m and any(kw in str(c).lower() for kw in ['nuance', 'nom', 'voix', 'elu', 'élu']):
                cand_nums.add(int(m.group(1)))

        rows = []
        for _, row in df.iterrows():
            dept_raw = str(row[dept_col])
            dept_label = str(row[dept_label_col])
            circo_raw = str(row[circo_col])

            dept_code = dept_raw.zfill(2) if dept_raw.isdigit() else dept_raw
            circo_num = circo_raw.zfill(2) if circo_raw.isdigit() else circo_raw
            id_circo = f"{dept_code}-{circo_num}"

            inscrits = _to_int(row.get(inscrits_col))
            votants = _to_int(row.get(votants_col))
            exprimes = _to_int(row.get(exprimes_col))
            blancs = _to_int(row.get(blancs_col))
            nuls = _to_int(row.get(nuls_col))

            # Si votants absent mais abstentions présentes
            if votants is None and inscrits is not None and abstentions_col:
                abstentions = _to_int(row.get(abstentions_col))
                if abstentions is not None:
                    votants = inscrits - abstentions

            for n in sorted(cand_nums):
                nom_col = next((c for c in headers if str(c) == f'Nom {n}'), None)
                prenom_col = next((c for c in headers if str(c) == f'Prénom {n}'), None)
                nuance_col = next((c for c in headers if str(c) in (f'Nuance {n}', f'nuance {n}')), None)
                voix_col = next((c for c in headers if str(c) in (f'Voix {n}', f'voix {n}')), None)
                elu_col = next((c for c in headers if str(c).lower() in (f'elu {n}', f'élu {n}')), None)

                if nom_col is None:
                    continue

                nom = str(row[nom_col]) if str(row[nom_col]) != 'nan' else None
                if nom is None:
                    continue

                prenom = str(row[prenom_col]) if prenom_col and str(row[prenom_col]) != 'nan' else ''
                nuance = str(row[nuance_col]) if nuance_col and str(row[nuance_col]) != 'nan' else None
                voix = _to_int(row[voix_col]) if voix_col else None

                elu = False
                if is_t2 and elu_col:
                    elu_raw = str(row[elu_col]).lower()
                    if 'élu' in elu_raw or 'elu' in elu_raw or elu_raw == '1' or elu_raw == 'true':
                        elu = True

                nom_complet = f"{nom} {prenom}".strip()

                rows.append({
                    'id_circo': id_circo,
                    'departement': dept_label,
                    'dept_code': dept_code,
                    'circo': circo_num,
                    'nom_candidat': nom_complet,
                    'etiquette': nuance,
                    'nuance': nuance,
                    'inscrits': inscrits,
                    'votants': votants,
                    'exprimes': exprimes,
                    'blancs': blancs,
                    'nuls': nuls,
                    'voix': voix,
                    'acces': '',
                    'elu': elu,
                })

        return pd.DataFrame(rows)

    df_t1 = parse_round(df_t1_raw, is_t2=False)
    df_t2 = parse_round(df_t2_raw, is_t2=True)

    return merge_tours_ministere(df_t1, df_t2, annee, has_elu_in_t2=True)


def merge_tours_ministere(df_t1, df_t2, annee, has_elu_in_t2=True):
    """Fusionne T1 et T2 pour les données Ministère."""
    if df_t1.empty:
        return pd.DataFrame()

    # Calculer qualifie_t2
    df_t1['qualifie_t2'] = df_t1.apply(
        lambda r: calculer_qualifie(r['voix'], r['inscrits'], r['exprimes'], annee),
        axis=1
    )

    if df_t2.empty:
        df_t1['voix_t2'] = None
        df_t1['inscrits_t2'] = None
        df_t1['votants_t2'] = None
        df_t1['blancs_t2'] = None
        df_t1['nuls_t2'] = None
        df_t1['exprimes_t2'] = None
        df_t1['maintenu_t2'] = None
        if 'elu' not in df_t1.columns:
            df_t1['elu'] = False
        return _rename_t1(df_t1)

    # Normaliser les noms pour le matching
    df_t1['nom_key'] = df_t1['nom_candidat'].str.upper().str.strip()
    df_t2['nom_key'] = df_t2['nom_candidat'].str.upper().str.strip()

    # Merge T1 et T2
    t2_agg = df_t2.rename(columns={
        'voix': 'voix_t2',
        'inscrits': 'inscrits_t2',
        'votants': 'votants_t2',
        'exprimes': 'exprimes_t2',
        'blancs': 'blancs_t2',
        'nuls': 'nuls_t2',
        'elu': 'elu_t2',
    })
    t2_cols = ['id_circo', 'nom_key', 'voix_t2', 'inscrits_t2', 'votants_t2',
               'exprimes_t2', 'blancs_t2', 'nuls_t2', 'elu_t2']
    t2_cols = [c for c in t2_cols if c in t2_agg.columns]
    t2_merge = t2_agg[t2_cols].drop_duplicates(subset=['id_circo', 'nom_key'])

    # Supprimer la colonne elu de T1 (toujours False) avant merge pour éviter conflit
    df_t1 = df_t1.drop(columns=['elu'], errors='ignore')
    df = df_t1.merge(t2_merge, on=['id_circo', 'nom_key'], how='left')

    # maintenu_t2
    def calc_maintenu(row):
        if not row['qualifie_t2']:
            return None
        if pd.notna(row.get('voix_t2')) and _to_int(row.get('voix_t2', 0)) and _to_int(row.get('voix_t2', 0)) > 0:
            return True
        return False

    df['maintenu_t2'] = df.apply(calc_maintenu, axis=1)

    # elu: récupéré du T2
    if 'elu_t2' in df.columns:
        df['elu'] = df['elu_t2'].fillna(False)
        df = df.drop(columns=['elu_t2'])
    else:
        df['elu'] = False

    df = df.drop(columns=['nom_key'], errors='ignore')
    return _rename_t1(df)


# ─── Utilitaires ─────────────────────────────────────────────────────────────

def _to_int(v):
    """Convertit une valeur en int, retourne None si impossible."""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    try:
        s = str(v).replace(',', '').replace(' ', '').strip()
        if s == '' or s == 'nan' or s == 'None':
            return None
        return int(float(s))
    except (ValueError, TypeError):
        return None


def load_cdsp_csv(path, annee, year_format):
    """Charge un CSV CDSP avec le bon encodage."""
    encodings = ['utf-8', 'latin-1', 'windows-1252']
    for enc in encodings:
        try:
            if year_format == 'parti':
                # 2 lignes de header
                df = pd.read_csv(path, encoding=enc, header=[0, 1], low_memory=False)
                # Aplatir multi-index
                df.columns = [str(c[0]) if 'Unnamed' in str(c[1]) else str(c[1])
                               for c in df.columns]
                return df, enc
            else:
                df = pd.read_csv(path, encoding=enc, low_memory=False)
                return df, enc
        except Exception:
            continue
    raise ValueError(f"Impossible de lire {path}")


# ─── Construction de la table finale ─────────────────────────────────────────

def build_resultats_table(annee, df_parsed):
    """Standardise un dataframe parsé vers le schéma cible."""
    if df_parsed is None or df_parsed.empty:
        return pd.DataFrame()

    # Colonnes cibles
    target_cols = [
        'annee', 'id_circo', 'departement',
        'nom_candidat', 'etiquette', 'nuance',
        'voix_t1', 'qualifie_t2', 'maintenu_t2', 'voix_t2', 'elu',
        'inscrits_t1', 'inscrits_t2', 'votants_t1', 'votants_t2',
        'blancs_t1', 'blancs_t2', 'nuls_t1', 'nuls_t2',
        'exprimes_t1', 'exprimes_t2',
    ]

    df = df_parsed.copy()
    df['annee'] = annee

    # Renommer si nécessaire
    rename = {}
    for col in ['inscrits', 'votants', 'exprimes', 'blancs', 'nuls']:
        if col in df.columns and f'{col}_t1' not in df.columns:
            rename[col] = f'{col}_t1'
    df = df.rename(columns=rename)

    # Assurer que toutes les colonnes cibles existent
    for col in target_cols:
        if col not in df.columns:
            df[col] = None

    # Convertir les types
    int_cols = ['voix_t1', 'voix_t2', 'inscrits_t1', 'inscrits_t2',
                'votants_t1', 'votants_t2', 'blancs_t1', 'blancs_t2',
                'nuls_t1', 'nuls_t2', 'exprimes_t1', 'exprimes_t2']
    for col in int_cols:
        if col in df.columns:
            df[col] = df[col].apply(_to_int)

    bool_cols = ['qualifie_t2', 'maintenu_t2', 'elu']
    for col in bool_cols:
        if col in df.columns:
            df[col] = df[col].apply(lambda x: None if x is None or (isinstance(x, float) and np.isnan(x)) else bool(x))

    return df[target_cols]


# ─── Pipeline principal ───────────────────────────────────────────────────────

def run_pipeline():
    """Pipeline complet."""
    all_results = []
    coverage = {}

    print("=" * 60)
    print("Pipeline élections législatives françaises 1958-2024")
    print("=" * 60)

    # ── CDSP 1958-2012 ──────────────────────────────────────────
    cdsp_years = [1958, 1962, 1967, 1968, 1973, 1978, 1981, 1988, 1993, 1997, 2002, 2007, 2012]

    for annee in cdsp_years:
        print(f"\n[CDSP] {annee}...")
        path_t1 = RAW_CDSP / f'cdsp_legi{annee}t1_circ.csv'
        path_t2 = RAW_CDSP / f'cdsp_legi{annee}t2_circ.csv'

        if not path_t1.exists():
            print(f"  SKIP: {path_t1} manquant")
            coverage[annee] = {'status': 'missing', 'rows': 0}
            continue

        try:
            # Détecter format automatiquement
            # Si les headers contiennent "Nom candidat" -> format candidat
            # Sinon -> format parti (données agrégées par parti)
            probe = pd.read_csv(path_t1, encoding='latin-1', nrows=1, low_memory=False)
            probe_cols = ' '.join(str(c) for c in probe.columns)
            if 'Nom candidat' in probe_cols or 'Nom 1' in probe_cols:
                year_format = 'candidat'
            else:
                year_format = 'parti'

            if year_format == 'parti':
                # Header double ligne
                df_t1_raw = pd.read_csv(path_t1, encoding='latin-1', header=[0,1], low_memory=False)
                df_t2_raw = pd.read_csv(path_t2, encoding='latin-1', header=[0,1], low_memory=False) if path_t2.exists() else None

                # Pour les années parti, on a seulement des données agrégées
                # On les traite en un format simplifié
                df_t1_simple = pd.read_csv(path_t1, encoding='latin-1', low_memory=False)
                df_t2_simple = pd.read_csv(path_t2, encoding='latin-1', low_memory=False) if path_t2.exists() else None

                df_parsed = parse_cdsp_partis_v2(df_t1_simple, df_t2_simple, annee)
            else:
                year_format = 'candidat'
                df_t1_raw = pd.read_csv(path_t1, encoding='latin-1', low_memory=False)
                df_t2_raw = pd.read_csv(path_t2, encoding='latin-1', low_memory=False) if path_t2.exists() else None
                df_parsed = parse_cdsp_candidats(df_t1_raw, df_t2_raw, annee)

            df_final = build_resultats_table(annee, df_parsed)

            n_circos = df_final['id_circo'].nunique() if not df_final.empty else 0
            n_rows = len(df_final)
            print(f"  OK: {n_rows} lignes, {n_circos} circos")
            coverage[annee] = {'status': 'ok', 'rows': n_rows, 'circos': n_circos, 'source': 'CDSP'}

            if not df_final.empty:
                all_results.append(df_final)

        except Exception as e:
            print(f"  ERREUR: {e}")
            import traceback
            traceback.print_exc()
            coverage[annee] = {'status': 'error', 'error': str(e), 'rows': 0}

    # ── Ministère 2017-2024 ──────────────────────────────────────
    min_years = [
        (2017, 'Leg_2017_Resultats_T1_c.xlsx', 'Leg_2017_Resultats_T2_c.xlsx'),
        (2022, 'Leg_2022_Resultats_T1.xlsx', 'Leg_2022_Resultats_T2.xlsx'),
        (2024, 'Leg_2024_Resultats_T1.xlsx', 'Leg_2024_Resultats_T2.xlsx'),
    ]

    for annee, fname_t1, fname_t2 in min_years:
        print(f"\n[Ministère] {annee}...")
        path_t1 = RAW_MIN / fname_t1
        path_t2 = RAW_MIN / fname_t2

        if not path_t1.exists():
            print(f"  SKIP: {path_t1} manquant")
            coverage[annee] = {'status': 'missing', 'rows': 0}
            continue

        try:
            df_parsed = parse_xlsx_ministere(path_t1, path_t2, annee)
            df_final = build_resultats_table(annee, df_parsed)

            n_circos = df_final['id_circo'].nunique() if not df_final.empty else 0
            n_rows = len(df_final)
            print(f"  OK: {n_rows} lignes, {n_circos} circos")
            coverage[annee] = {'status': 'ok', 'rows': n_rows, 'circos': n_circos, 'source': 'Ministère'}

            if not df_final.empty:
                all_results.append(df_final)

        except Exception as e:
            print(f"  ERREUR: {e}")
            import traceback
            traceback.print_exc()
            coverage[annee] = {'status': 'error', 'error': str(e), 'rows': 0}

    # ── Consolidation ────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("Consolidation...")

    if not all_results:
        print("ERREUR: Aucune donnée à consolider")
        return coverage

    df_all = pd.concat(all_results, ignore_index=True)
    print(f"Total: {len(df_all)} lignes, {df_all['annee'].nunique()} élections")

    # Sauvegarder CSV et Parquet
    CLEAN_DIR.mkdir(parents=True, exist_ok=True)

    csv_path = CLEAN_DIR / 'resultats_complets.csv'
    df_all.to_csv(csv_path, index=False)
    print(f"CSV sauvegardé: {csv_path}")

    parquet_path = CLEAN_DIR / 'resultats_complets.parquet'
    df_all.to_parquet(parquet_path, index=False)
    print(f"Parquet sauvegardé: {parquet_path}")

    # ── SQLite ───────────────────────────────────────────────────
    print("\nConstruction SQLite...")
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS resultats (
        annee           INTEGER,
        id_circo        TEXT,
        departement     TEXT,
        nom_candidat    TEXT,
        etiquette       TEXT,
        nuance          TEXT,
        voix_t1         INTEGER,
        qualifie_t2     INTEGER,  -- 0/1/NULL (SQLite n'a pas BOOLEAN)
        maintenu_t2     INTEGER,  -- NULL si non qualifié
        voix_t2         INTEGER,
        elu             INTEGER,
        inscrits_t1     INTEGER,
        inscrits_t2     INTEGER,
        votants_t1      INTEGER,
        votants_t2      INTEGER,
        blancs_t1       INTEGER,
        blancs_t2       INTEGER,
        nuls_t1         INTEGER,
        nuls_t2         INTEGER,
        exprimes_t1     INTEGER,
        exprimes_t2     INTEGER
    )
    """)

    conn.execute("DELETE FROM resultats")

    # Convertir booleans pour SQLite
    df_sqlite = df_all.copy()
    for col in ['qualifie_t2', 'maintenu_t2', 'elu']:
        df_sqlite[col] = df_sqlite[col].apply(
            lambda x: None if x is None else (1 if x else 0)
        )

    df_sqlite.to_sql('resultats', conn, if_exists='append', index=False)
    conn.commit()

    # Créer index
    conn.execute("CREATE INDEX IF NOT EXISTS idx_annee ON resultats(annee)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_circo ON resultats(id_circo)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_annee_circo ON resultats(annee, id_circo)")
    conn.commit()

    print(f"SQLite sauvegardé: {DB_PATH}")

    # ── Triangulaires ────────────────────────────────────────────
    print("\nCalcul des triangulaires et quadrangulaires...")

    triangulaires = compute_triangulaires(conn)
    tri_path = CLEAN_DIR / 'triangulaires_1958_2024.csv'
    triangulaires.to_csv(tri_path, index=False)
    print(f"Triangulaires sauvegardées: {tri_path}")

    conn.close()

    return coverage, df_all, triangulaires


def parse_cdsp_partis_v2(df_t1_raw, df_t2_raw, annee):
    """
    Parse format parti CDSP (1958-1968).
    La ligne 1 contient les codes nuances.
    """
    def parse_round(df_raw, is_t2=False):
        if df_raw is None:
            return pd.DataFrame()

        # Row 0 = noms longs des partis (header)
        # Row 1 = codes nuances
        headers = list(df_raw.columns)  # noms longs
        nuance_codes = list(df_raw.iloc[0])  # codes nuances

        # Colonnes meta = les 6-7 premières
        meta_end = 0
        for i, h in enumerate(headers):
            h_low = str(h).lower()
            if 'blancs' in h_low or 'nuls' in h_low:
                meta_end = i + 1
                break

        party_headers = headers[meta_end:]
        party_codes = [str(c) for c in nuance_codes[meta_end:]]

        rows = []
        for _, row in df_raw.iloc[1:].iterrows():
            dept_raw = str(row.iloc[0])
            dept_code = dept_raw.zfill(2) if dept_raw.isdigit() else dept_raw
            departement = str(row.iloc[1])
            circo_raw = str(row.iloc[2])
            circo_num = circo_raw.zfill(2) if circo_raw.isdigit() else circo_raw
            id_circo = f"{dept_code}-{circo_num}"

            inscrits = _to_int(row.iloc[3])
            votants = _to_int(row.iloc[4])
            exprimes = _to_int(row.iloc[5])
            blancs_nuls = _to_int(row.iloc[meta_end - 1]) if meta_end > 5 else None

            for ph, pc in zip(party_headers, party_codes):
                voix = _to_int(row[ph])
                if voix is None or voix == 0:
                    continue

                rows.append({
                    'id_circo': id_circo,
                    'departement': departement,
                    'dept_code': dept_code,
                    'circo': circo_num,
                    'nom_candidat': f"[{pc}]",
                    'etiquette': str(ph)[:100],
                    'nuance': pc if pc != 'nan' else str(ph)[:10],
                    'inscrits': inscrits,
                    'votants': votants,
                    'exprimes': exprimes,
                    'blancs': blancs_nuls,
                    'nuls': None,
                    'voix': voix,
                    'acces': '',
                })

        return pd.DataFrame(rows)

    df_t1 = parse_round(df_t1_raw, is_t2=False)
    df_t2 = parse_round(df_t2_raw, is_t2=True)

    return merge_tours(df_t1, df_t2, annee)


def compute_triangulaires(conn):
    """Calcule les triangulaires (circos avec 3+ candidats qualifiés au T2)."""
    query = """
    SELECT annee, id_circo, departement,
           COUNT(*) as nb_qualifies,
           SUM(CASE WHEN maintenu_t2 = 1 THEN 1 ELSE 0 END) as nb_maintenus,
           GROUP_CONCAT(nuance || ':' || COALESCE(voix_t1, 0), '; ') as candidats
    FROM resultats
    WHERE qualifie_t2 = 1
    GROUP BY annee, id_circo
    HAVING nb_maintenus >= 3
    ORDER BY annee, id_circo
    """
    df = pd.read_sql(query, conn)
    return df


# ─── Entrée principale ────────────────────────────────────────────────────────

if __name__ == '__main__':
    result = run_pipeline()
    if isinstance(result, tuple):
        coverage, df_all, triangulaires = result
    else:
        coverage = result
        df_all = None
        triangulaires = None

    print("\n" + "=" * 60)
    print("RAPPORT DE COUVERTURE")
    print("=" * 60)
    for annee in sorted(coverage.keys()):
        info = coverage[annee]
        status = info['status']
        if status == 'ok':
            print(f"  {annee}: OK - {info['rows']} lignes ({info.get('circos', '?')} circos) [{info.get('source', '?')}]")
        elif status == 'missing':
            print(f"  {annee}: MANQUANT")
        else:
            print(f"  {annee}: ERREUR - {info.get('error', '?')}")

    if triangulaires is not None and not triangulaires.empty:
        print("\n" + "=" * 60)
        print("TRIANGULAIRES PAR ANNÉE")
        print("=" * 60)
        tri_by_year = triangulaires.groupby('annee').size()
        for annee, count in tri_by_year.items():
            print(f"  {annee}: {count} circos avec 3+ maintenus au T2")
