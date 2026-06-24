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

def _normalize_code(val, width=2):
    """Normalise un code département ou circonscription en chaîne zero-paddée.
    Gère les artefacts float pandas ('1.0' → '01', '12.0' → '12')."""
    s = str(val).strip()
    if s in ('nan', 'None', ''):
        return s
    # Artefact float : '1.0', '12.0' etc.
    try:
        f = float(s)
        if f == int(f):
            return str(int(f)).zfill(width)
    except ValueError:
        pass
    return s.zfill(width) if s.isdigit() else s


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
            dept_code = _normalize_code(row.iloc[0], 2)
            departement = str(row.iloc[1])
            circo = _normalize_code(row.iloc[2], 2)
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
            dept_code = _normalize_code(row.iloc[0], 2)
            departement = str(row.iloc[1])
            circo = _normalize_code(row.iloc[2], 2)
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


def parse_xls_ministere_2002_2012(path, annee):
    """
    Parse les fichiers XLS du Ministère pour 2002, 2007, 2012.
    Format: onglets 'Circo leg T1'/'Circo Leg T1', 'Circo leg T2', 'Elus'
    Structure large: une ligne = une circonscription, candidats en colonnes répétées.
    Colonnes 0-14: meta (dept, circo, inscrits, votants, exprimés, blancs+nuls)
    Colonnes 15+: blocs de 7 par candidat (Sexe, Nom, Prénom, Nuance, Voix, %/Ins, %/Exp)
    """
    import xlrd

    wb = xlrd.open_workbook(str(path))
    sheet_names = wb.sheet_names()

    def get_sheet(wb, candidates):
        for name in candidates:
            if name in sheet_names:
                return wb.sheet_by_name(name)
        return None

    sh_t1 = get_sheet(wb, ['Circo leg T1', 'Circo Leg T1'])
    sh_t2 = get_sheet(wb, ['Circo leg T2', 'Circo Leg T2'])
    sh_elu = get_sheet(wb, ['Elus'])

    def parse_sheet_wide(sh):
        if sh is None:
            return pd.DataFrame()
        headers = [str(sh.cell_value(0, j)) for j in range(sh.ncols)]
        rows = []
        for i in range(1, sh.nrows):
            dept_raw = _normalize_code(sh.cell_value(i, 0))
            dept_label = str(sh.cell_value(i, 1)).strip()
            circo_raw = _normalize_code(sh.cell_value(i, 2))
            id_circo = f"{dept_raw}-{circo_raw}"

            inscrits = _to_int(sh.cell_value(i, 4))
            votants_raw = sh.cell_value(i, 7)
            abstentions_raw = sh.cell_value(i, 5)
            inscrits_v = inscrits
            votants = _to_int(votants_raw)
            if votants is None and inscrits is not None:
                abs_v = _to_int(abstentions_raw)
                if abs_v is not None:
                    votants = inscrits - abs_v
            blancs_nuls = _to_int(sh.cell_value(i, 9))
            exprimes = _to_int(sh.cell_value(i, 12))

            # Candidats en blocs de 7 à partir de col 15
            j = 15
            while j + 4 < sh.ncols:
                nom = str(sh.cell_value(i, j + 1)).strip()
                if not nom or nom in ('nan', 'None', ''):
                    j += 7
                    continue
                prenom = str(sh.cell_value(i, j + 2)).strip()
                nuance = str(sh.cell_value(i, j + 3)).strip()
                voix = _to_int(sh.cell_value(i, j + 4))
                nom_complet = f"{nom} {prenom}".strip()
                rows.append({
                    'id_circo': id_circo,
                    'departement': dept_label,
                    'nom_candidat': nom_complet,
                    'etiquette': nuance,
                    'nuance': nuance,
                    'inscrits': inscrits_v,
                    'votants': votants,
                    'blancs': blancs_nuls,
                    'nuls': None,
                    'exprimes': exprimes,
                    'voix': voix,
                    'elu': False,
                })
                j += 7
        return pd.DataFrame(rows)

    # Lire les élu(e)s depuis l'onglet Elus
    elus_set = set()  # (id_circo,) -> True si élu en T2 (on utilisera voix max en T2)
    elus_t1 = set()  # circos where winner is elected at T1
    if sh_elu is not None:
        for i in range(1, sh_elu.nrows):
            dept_raw = _normalize_code(sh_elu.cell_value(i, 0))
            circo_raw = _normalize_code(sh_elu.cell_value(i, 2))
            id_circo = f"{dept_raw}-{circo_raw}"
            tour = _to_int(sh_elu.cell_value(i, 7))
            if tour == 1:
                elus_t1.add(id_circo)

    df_t1 = parse_sheet_wide(sh_t1)
    df_t2 = parse_sheet_wide(sh_t2)

    if df_t1.empty:
        return pd.DataFrame()

    # Calculer qualifie_t2
    df_t1['qualifie_t2'] = df_t1.apply(
        lambda r: calculer_qualifie(r['voix'], r['inscrits'], r['exprimes'], annee),
        axis=1
    )

    # Marquer les élus au T1 (gagnent sans T2)
    df_t1['elu'] = df_t1['id_circo'].isin(elus_t1)

    if df_t2.empty:
        df_t1['voix_t2'] = None
        df_t1['inscrits_t2'] = None
        df_t1['votants_t2'] = None
        df_t1['blancs_t2'] = None
        df_t1['nuls_t2'] = None
        df_t1['exprimes_t2'] = None
        df_t1['maintenu_t2'] = None
        return _rename_t1(df_t1)

    # Identifier l'élu en T2: candidat avec le plus de voix en T2 dans chaque circo
    # sauf pour les circos où l'élu est au T1
    t2_max = df_t2[~df_t2['id_circo'].isin(elus_t1)].copy()
    if not t2_max.empty:
        idx_max = t2_max.groupby('id_circo')['voix'].idxmax()
        elu_idx = set(idx_max.values)
        df_t2['elu'] = df_t2.index.isin(elu_idx)

    # Merge T1 et T2 sur (id_circo, nom_candidat normalisé)
    df_t1['nom_key'] = df_t1['nom_candidat'].str.upper().str.strip()
    df_t2['nom_key'] = df_t2['nom_candidat'].str.upper().str.strip()

    t2_rename = df_t2.rename(columns={
        'voix': 'voix_t2', 'inscrits': 'inscrits_t2', 'votants': 'votants_t2',
        'exprimes': 'exprimes_t2', 'blancs': 'blancs_t2', 'nuls': 'nuls_t2',
        'elu': 'elu_t2',
    })
    t2_cols = ['id_circo', 'nom_key', 'voix_t2', 'inscrits_t2', 'votants_t2',
               'exprimes_t2', 'blancs_t2', 'nuls_t2', 'elu_t2']
    t2_cols = [c for c in t2_cols if c in t2_rename.columns]
    t2_merge = t2_rename[t2_cols].drop_duplicates(subset=['id_circo', 'nom_key'])

    df_t1_noelu = df_t1.drop(columns=['elu'], errors='ignore')
    df = df_t1_noelu.merge(t2_merge, on=['id_circo', 'nom_key'], how='left')

    def calc_maintenu(row):
        if not row['qualifie_t2']:
            return None
        v2 = _to_int(row.get('voix_t2'))
        if v2 is not None and v2 > 0:
            return True
        return False

    df['maintenu_t2'] = df.apply(calc_maintenu, axis=1)

    if 'elu_t2' in df.columns:
        df['elu'] = df['elu_t2'].fillna(False)
        df['elu'] = df.apply(lambda r: True if r['id_circo'] in elus_t1 and r['voix'] == df_t1[df_t1['id_circo'] == r['id_circo']]['voix'].max() else r['elu'], axis=1)
        df = df.drop(columns=['elu_t2'])
    else:
        df['elu'] = False

    df = df.drop(columns=['nom_key'], errors='ignore')
    return _rename_t1(df)


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

    # ── CDSP 1958-1997 ──────────────────────────────────────────
    cdsp_years = [1958, 1962, 1967, 1968, 1973, 1978, 1981, 1988, 1993, 1997]

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

    # ── Ministère 2002-2012 (XLS format large circo) ────────────
    min_xls_years = [
        (2002, 'Leg_2002_Resultats.xls'),
        (2007, 'Leg_2007_Resultats_v2.xls'),
        (2012, 'Leg_2012_Resultats.xls'),
    ]

    for annee, fname in min_xls_years:
        print(f"\n[Ministère] {annee}...")
        path = RAW_MIN / fname

        if not path.exists():
            print(f"  SKIP: {path} manquant")
            coverage[annee] = {'status': 'missing', 'rows': 0}
            continue

        try:
            df_parsed = parse_xls_ministere_2002_2012(path, annee)
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
    Parse format parti CDSP (1958-1981). Données agrégées par parti, pas par candidat.

    Formats détectés :
    - 1958-1978 : 2 lignes de header (long names + codes), T2 contient colonne 'élu premier tour'
    - 1981 : 1 ligne de header (codes directs), T2 ne liste que les circos en ballottage
    """
    # Correspondance codes T2 → codes T1 quand ils diffèrent (cas 1968 principalement)
    T2_TO_T1_CODE = {
        1968: {'PC': 'COM', 'UDR-RI': 'UDR', 'DIVMAJ': 'DIVGAULL', 'DVD': 'DIVGAULL'},
    }

    def detect_header_rows(df_raw):
        """Retourne (nb_header_rows, has_elu_col, data_start_col_offset).
        1981 T2 : 1 seule ligne de header, pas de colonne élu.
        1958-1978 : 2 lignes de header, colonne élu premier tour en T2."""
        headers = list(df_raw.columns)
        # Si la première colonne ressemble à un code ou un numéro → 1 header row (1981)
        first_col = str(headers[0]).lower()
        if 'code' not in first_col and 'département' not in first_col and 'dep' not in first_col:
            return 1, False
        # Cherche colonne élu premier tour dans les headers
        has_elu = any('lu' in str(h).lower() and 'tour' in str(h).lower() for h in headers)
        return 2, has_elu

    def parse_round(df_raw, is_t2=False):
        if df_raw is None:
            return pd.DataFrame()

        headers = list(df_raw.columns)
        nb_headers, has_elu = detect_header_rows(df_raw)

        if nb_headers == 2:
            # Row 0 of df = nuance codes (2nd header row of CSV)
            nuance_codes = list(df_raw.iloc[0])
            data_rows = df_raw.iloc[1:]
        else:
            # 1981 : codes sont dans df.columns directement, données dès la première ligne
            nuance_codes = headers
            data_rows = df_raw

        # Trouver où se terminent les colonnes meta (avant les colonnes parti)
        meta_end = 0
        elu_col_idx = None
        for i, h in enumerate(headers):
            h_low = str(h).lower()
            if 'lu' in h_low and 'tour' in h_low:
                elu_col_idx = i
            if 'blancs' in h_low or 'nuls' in h_low:
                meta_end = i + 1
                break

        if meta_end == 0:
            # 1981 T2 : pas de colonne Blancs et nuls — meta cols jusqu'à 'Exprimés'
            for i, h in enumerate(headers):
                if 'exprim' in str(h).lower():
                    meta_end = i + 1
                    break

        party_headers = headers[meta_end:]
        party_codes_raw = [str(c) for c in nuance_codes[meta_end:]]

        # Déduplication : si plusieurs colonnes ont le même code nuance dans le même circo,
        # on les distingue par index pour éviter les faux matchs au merge
        seen_codes = {}
        party_codes = []
        for pc in party_codes_raw:
            if pc == 'nan' or pc == '':
                party_codes.append(pc)
                continue
            if pc in seen_codes:
                seen_codes[pc] += 1
                party_codes.append(f"{pc}#{seen_codes[pc]}")
            else:
                seen_codes[pc] = 0
                party_codes.append(pc)

        rows = []
        for _, row in data_rows.iterrows():
            dept_raw = str(row.iloc[0])
            if dept_raw in ('nan', 'None', ''):
                continue
            dept_code = _normalize_code(dept_raw, 2)
            departement = str(row.iloc[1])
            circo_raw = str(row.iloc[2])
            if circo_raw in ('nan', 'None', ''):
                continue
            circo_num = _normalize_code(circo_raw, 2)
            id_circo = f"{dept_code}-{circo_num}"

            # Skip élu premier tour = 'O'
            if elu_col_idx is not None:
                elu_val = str(row.iloc[elu_col_idx]).strip().upper()
                if elu_val == 'O':
                    continue

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
                    'nuance': pc if pc not in ('nan', '') else str(ph)[:10],
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

    if df_t2.empty:
        return merge_tours(df_t1, pd.DataFrame(), annee)

    # Remapper les codes T2 → T1 pour les années avec divergences (ex: 1968 PC→COM)
    code_map = T2_TO_T1_CODE.get(annee, {})
    if code_map and not df_t2.empty:
        def remap(nc):
            base = nc.split('#')[0]  # retire le suffixe de déduplication
            mapped = code_map.get(base, base)
            return f"[{mapped}]"
        df_t2['nom_candidat'] = df_t2['nom_candidat'].apply(
            lambda nc: remap(nc) if nc.startswith('[') else nc
        )

    # Agréger les lignes de même (id_circo, base_nuance_code) pour éviter de compter
    # deux fois les partis avec des colonnes dupliquées (ex: 1967 deux colonnes 'COM')
    # On garde le nom_candidat sans suffixe #N et on somme les voix.
    def aggregate_dupes(df):
        if df.empty:
            return df
        df = df.copy()
        df['_base_nom'] = df['nom_candidat'].str.replace(r'#\d+$', '', regex=True)
        meta = ['id_circo', 'departement', 'dept_code', 'circo',
                'etiquette', 'nuance', 'inscrits', 'votants', 'exprimes', 'blancs']
        agg = df.groupby(['id_circo', '_base_nom'], as_index=False).agg(
            voix=('voix', 'sum'),
            **{c: (c, 'first') for c in meta if c in df.columns}
        )
        agg['nom_candidat'] = agg['_base_nom']
        agg = agg.drop(columns=['_base_nom'])
        # Garder aussi 'nuls' et 'acces' si présents
        for c in ['nuls', 'acces']:
            if c in df.columns:
                agg[c] = df.groupby(['id_circo', '_base_nom'])[c].first().values
        return agg

    df_t1 = aggregate_dupes(df_t1)
    df_t2 = aggregate_dupes(df_t2)

    # Pour les T2 entries qui ne trouvent pas de correspondance en T1, on les ajoute quand même
    # comme nouvelles lignes (avec voix_t1=NULL) pour ne pas perdre la config T2
    t1_keys = set(zip(df_t1['id_circo'], df_t1['nom_candidat']))
    t2_unmatched = df_t2[~df_t2.apply(
        lambda r: (r['id_circo'], r['nom_candidat']) in t1_keys, axis=1
    )].copy()

    merged = merge_tours(df_t1, df_t2, annee)

    # Ajouter les T2 non-matchés comme lignes supplémentaires avec voix_t1=NULL
    if not t2_unmatched.empty:
        pct, base = seuil_qualification(annee)
        extra_rows = []
        for _, r in t2_unmatched.iterrows():
            extra_rows.append({
                'id_circo': r['id_circo'],
                'departement': r['departement'],
                'nom_candidat': r['nom_candidat'],
                'etiquette': r['etiquette'],
                'nuance': r['nuance'],
                'voix_t1': None,
                'qualifie_t2': False,
                'maintenu_t2': True,
                'voix_t2': r['voix'],
                'elu': False,
                'inscrits_t1': r['inscrits'],
                'inscrits_t2': r['inscrits'],
                'votants_t1': r['votants'],
                'votants_t2': r['votants'],
                'blancs_t1': r['blancs'],
                'blancs_t2': r['blancs'],
                'nuls_t1': None,
                'nuls_t2': None,
                'exprimes_t1': r['exprimes'],
                'exprimes_t2': r['exprimes'],
            })
        if extra_rows:
            df_extra = pd.DataFrame(extra_rows)
            merged = pd.concat([merged, df_extra], ignore_index=True)

    return merged


def compute_triangulaires(conn):
    """Exporte les candidats présents au T2 dans les circos à 3+ candidats (triangulaires+)."""
    # Compter le nombre de candidats ayant voté au T2 par circo
    config_query = """
    SELECT annee, id_circo, COUNT(*) as config_t2
    FROM resultats
    WHERE voix_t2 IS NOT NULL AND voix_t2 > 0
    GROUP BY annee, id_circo
    HAVING config_t2 >= 3
    """
    # Récupérer toutes les lignes des candidats maintenus dans ces circos
    query = """
    SELECT r.annee, r.id_circo, r.departement, r.nom_candidat, r.etiquette, r.nuance,
           r.voix_t1, r.voix_t2, r.elu, c.config_t2
    FROM resultats r
    JOIN (
        SELECT annee, id_circo, COUNT(*) as config_t2
        FROM resultats
        WHERE voix_t2 IS NOT NULL AND voix_t2 > 0
        GROUP BY annee, id_circo
        HAVING config_t2 >= 3
    ) c ON r.annee = c.annee AND r.id_circo = c.id_circo
    WHERE r.voix_t2 IS NOT NULL AND r.voix_t2 > 0
    ORDER BY r.annee, r.id_circo, r.voix_t2 DESC
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
