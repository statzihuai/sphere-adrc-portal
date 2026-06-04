#!/usr/bin/env python3
"""
scripts/inject_data.py
Generate data/adrc_data.js from SPHERE CSV files in data/sphere/.

Outputs window.ADRC_DATA={...} using actual SPHERE column names.
Works via both http:// and file://.

Usage (from project root):
    python3 scripts/inject_data.py
"""
import csv, json, sys, shutil
from pathlib import Path

ROOT       = Path(__file__).resolve().parent.parent          # portal/
SPHERE     = ROOT.parent / "sphere_xk" / "x2"               # final_code_Apr20/sphere_xk/x2/
SPHERE_DIR = ROOT / "data" / "sphere"                        # portal/data/sphere/  (self-contained copies)
OUT_JS     = ROOT / "data" / "adrc_data.js"

# ── copy raw CSV files into portal/data/sphere/ ────────────────────────────
# Local copies for development only. The hosted portal serves files from SDR:
#   https://purl.stanford.edu/sm297vv5829
# This runs first so that SPHERE_DIR is up-to-date before we read from it.
SPHERE_DIR.mkdir(parents=True, exist_ok=True)
_COPIES = {
    "demographics_diagnosis.csv": SPHERE / "demographics_diagnosis" / "demographics_diagnosis.csv",
    "cognitive_scores.csv":       SPHERE / "cognitive_scores"       / "cognitive_scores_c2_b4.csv",
    "biomarkers.csv":             SPHERE / "biomarkers"             / "biomarkers.csv",
    "imaging_amyloid.csv":        SPHERE / "imaging_phenotypes"     / "imaging_amyloid.csv",
    "imaging_tau.csv":            SPHERE / "imaging_phenotypes"     / "imaging_tau.csv",
    "wgs.csv":                    SPHERE / "wgs"                    / "wgs_genodata.csv",
    "proteomics_csf.csv":         SPHERE / "proteomics"             / "proteomics_somalogic_csf.csv",
    "proteomics_plasma.csv":      SPHERE / "proteomics"             / "proteomics_somalogic_plasma.csv",
    "scrna.csv":                  SPHERE / "rna_seq"                / "sc_rna_seq_cellexp.csv",
}
for _dest_name, _src in _COPIES.items():
    _dest = SPHERE_DIR / _dest_name
    if _src.exists():
        shutil.copy2(_src, _dest)
print(f"  sphere/ files copied from sphere_xk/x2")

# Map full diagnosis_consensus strings to short portal codes.
DIAG_MAP = {
    "Healthy Control":                                    "HC",
    "Mild Cognitive Impairment":                          "MCI",
    "Probable Alzheimers Disease":                        "AD",
    "Possible Alzheimers Disease":                        "AD",
    "Possible Alzheimers Disease ; Other":                "AD",
    "Possible Alzheimers Disease ; Lewy Body Disease":    "AD",
    "Parkinsons Disease only":                            "PD",
    "Parkinsons Disease and Mild cognitive impairment":   "PDMCI",
    "Parkinsons Disease and Dementia":                    "PDD",
    "Lewy Body Disease":                                  "LBD",
    "Other":                                              "Other",
    "unknown":                                            "Other",
}

def _read(path):
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def _f(v):
    if v is None or str(v).strip() in ("", "nan", "NaN", "None"):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None

def _clip_and_round(rows):
    """Clip numeric columns to [1st, 99th] percentile; round integer-like columns."""
    if not rows:
        return rows
    from collections import defaultdict
    col_vals = defaultdict(list)
    for r in rows:
        for k, v in r.items():
            if isinstance(v, float):
                col_vals[k].append(v)
    clip_lo, clip_hi, is_int = {}, {}, {}
    for col, vals in col_vals.items():
        arr = sorted(vals)
        n = len(arr)
        clip_lo[col] = arr[max(0, int(n * 0.01))]
        clip_hi[col] = arr[min(n - 1, int(n * 0.99))]
        is_int[col]  = all(abs(v - round(v)) < 1e-9 for v in vals)
    result = []
    for r in rows:
        new_r = {}
        for k, v in r.items():
            if isinstance(v, float) and k in clip_lo:
                v = max(clip_lo[k], min(clip_hi[k], v))
                if is_int.get(k):
                    v = int(round(v))
            new_r[k] = v
        result.append(new_r)
    return result

# ── demographics / diagnosis — read from portal/data/sphere/ ──────────────
# Columns: adrc_id, ethnicity, race, sex, diagnosis_consensus, age_at_visit
_dem_rows = _read(SPHERE_DIR / "demographics_diagnosis.csv")

demo_by_id: dict = {}
age_by_id:  dict = {}

for _row in _dem_rows:
    _pid = _row.get("adrc_id", "")
    if not _pid:
        continue
    demo_by_id[_pid] = {
        "sex":                 _row.get("sex", ""),
        "race":                _row.get("race", ""),
        "ethnicity":           _row.get("ethnicity", ""),
        "diagnosis_consensus": _row.get("diagnosis_consensus", ""),
    }
    _age = _f(_row.get("age_at_visit"))
    if _age is not None:
        age_by_id[_pid] = _age

all_ids = sorted(demo_by_id)
print(f"  {len(all_ids)} participants")

diag_lookup = {}
for pid in all_ids:
    full = str(demo_by_id.get(pid, {}).get("diagnosis_consensus", "Unknown"))
    diag_lookup[pid] = DIAG_MAP.get(full, full)

# ── load modality files from portal/data/sphere/ ──────────────────────────
bio_rows   = _read(SPHERE_DIR / "biomarkers.csv")
cog_rows   = _read(SPHERE_DIR / "cognitive_scores.csv")
amy_rows   = _read(SPHERE_DIR / "imaging_amyloid.csv")
tau_rows   = _read(SPHERE_DIR / "imaging_tau.csv")
wgs_rows   = _read(SPHERE_DIR / "wgs.csv")
scrna_rows = _read(SPHERE_DIR / "scrna.csv")
csf_rows   = _read(SPHERE_DIR / "proteomics_csf.csv")
plsm_rows  = _read(SPHERE_DIR / "proteomics_plasma.csv")

bio_by_id   = {r["adrc_id"]: r for r in bio_rows}
cog_by_id   = {r["adrc_id"]: r for r in cog_rows}
wgs_by_id   = {r["adrc_id"]: r for r in wgs_rows}
scrna_by_id = {r["adrc_id"]: r for r in scrna_rows}
csf_by_id   = {r["adrc_id"]: r for r in csf_rows}
plsm_by_id  = {r["adrc_id"]: r for r in plsm_rows}

# Imaging: one row per participant per file
amy_by_id  = {r["adrc_id"]: r for r in reversed(amy_rows)}
tau_by_id  = {r["adrc_id"]: r for r in reversed(tau_rows)}

# Modality membership
has_bio     = {r["adrc_id"] for r in bio_rows}
has_cog     = {r["adrc_id"] for r in cog_rows}
has_img_amy = {r["adrc_id"] for r in amy_rows}
has_img_tau = {r["adrc_id"] for r in tau_rows}
has_img     = has_img_amy | has_img_tau
has_wgs     = {r["adrc_id"] for r in wgs_rows}
has_scrna   = {r["adrc_id"] for r in scrna_rows}
has_csf     = {r["adrc_id"] for r in csf_rows}
has_plsm    = {r["adrc_id"] for r in plsm_rows}

# ── imaging: all columns from amyloid + tau PET files ─────────────────────
AMY_ALL_COLS = [c for c in (amy_rows[0].keys() if amy_rows else []) if c != "adrc_id"]
TAU_ALL_COLS = [c for c in (tau_rows[0].keys() if tau_rows else []) if c != "adrc_id"]

# ── WGS: all SNP columns ──────────────────────────────────────────────────
WGS_ALL_COLS = [c for c in (wgs_rows[0].keys() if wgs_rows else []) if c != "adrc_id"]

# ── scRNA: AD-relevant marker genes per cell type ─────────────────────────
SCRNA_COLS = []
if scrna_rows:
    _all = set(scrna_rows[0].keys())
    # Priority genes per cell type
    candidates = [
        "CST3_CD14_Monocyte","LYZ_CD14_Monocyte","S100A8_CD14_Monocyte",
        "FCGR3A_CD16_Monocyte","CX3CR1_CD16_Monocyte","LST1_CD16_Monocyte",
        "IL7R_CD4_T_cell","TCF7_CD4_T_cell","CCR7_CD4_T_cell",
        "CD8A_CD8_T_cell","GZMK_CD8_T_cell","NKG7_CD8_T_cell",
        "GNLY_NK_cell","KLRD1_NK_cell","XCL1_NK_cell",
        "MS4A1_Mature_B_cell","CD79A_Mature_B_cell","CD22_Mature_B_cell",
        "VPREB3_Immature_B_cell","IGLL1_Immature_B_cell",
        "FCER1A_Dendritic_cell","CLEC10A_Dendritic_cell",
    ]
    # Only keep those actually in the file; fill remaining slots from available
    SCRNA_COLS = [c for c in candidates if c in _all]
    if len(SCRNA_COLS) < 6:
        SCRNA_COLS += [c for c in _all if c != "adrc_id" and c not in SCRNA_COLS][:12-len(SCRNA_COLS)]

# ── proteomics: curated aptamers for in-portal display/CSV ────────────────
# Full 5285-column CSF and 6901-column plasma data are too large for browser memory.
# The complete raw CSV files are copied to data/sphere/ for direct download.
# Here we include the DS_KEY_VARS display columns plus common AD/neurodegeneration markers.
_CSF_CANDIDATES = [
    "NEFL.10082.251.3","GFAP.3034.1.2","BDNF.14047.78.3","TNF.5936.53.3","CRP.4337.49.2",
    "IL6.4673.23.3","IL18.18.1.3","VEGFA.2597.7.3","TREM2.8456.1.3","CLU.4479.47.3",
    "APOE.6680.8.3","APP.4479.47.3","PSEN1.5965.3.3","BACE1.4479.47.3","MAPT.8456.1.3",
    "CD33.4479.47.3","BIN1.6680.8.3","ABCA7.8456.1.3","CR1.2630.18.2","MS4A6A.8456.1.3",
]
_PLSM_CANDIDATES = [
    "NEFL.10082.251.3","IL6.IL6R.21946.79.3","GFAP.3034.1.2","BDNF.14047.78.3",
    "TNF.5936.53.3","CRP.4337.49.2","IL18.18.1.3","VEGFA.2597.7.3","TREM2.8456.1.3",
    "CLU.4479.47.3","APOE.6680.8.3","APP.4479.47.3","CD33.4479.47.3","BIN1.6680.8.3",
    "ABCA7.8456.1.3","CR1.2630.18.2","PSEN1.5965.3.3","BACE1.4479.47.3",
]
_csf_avail  = set(csf_rows[0].keys())  if csf_rows  else set()
_plsm_avail = set(plsm_rows[0].keys()) if plsm_rows else set()
CSF_PROT_COLS  = [c for c in _CSF_CANDIDATES  if c in _csf_avail]
PLSM_PROT_COLS = [c for c in _PLSM_CANDIDATES if c in _plsm_avail]
# Fall back to first N columns if candidates don't match
if len(CSF_PROT_COLS)  < 5:
    CSF_PROT_COLS  = [c for c in (csf_rows[0].keys()  if csf_rows  else []) if c != "adrc_id"][:50]
if len(PLSM_PROT_COLS) < 5:
    PLSM_PROT_COLS = [c for c in (plsm_rows[0].keys() if plsm_rows else []) if c != "adrc_id"][:50]

# ── build output ───────────────────────────────────────────────────────────
participants = []
cognitive    = []
imaging      = []
wgs_out      = []
scrna_out    = []
csf_out      = []
plasma_out   = []
bio_out      = []

for pid in all_ids:
    dem  = demo_by_id.get(pid, {})
    diag = diag_lookup[pid]

    sex_raw = str(dem.get("sex","")).strip().lower()
    sex       = "M" if sex_raw in ("m","male") else ("F" if sex_raw in ("f","female") else sex_raw or None)
    age       = age_by_id.get(pid)
    race      = str(dem.get("race","")).strip() or None
    ethnicity = str(dem.get("ethnicity","")).strip() or None

    ds_list = sorted(set(
        (["biomarkers"]      if pid in has_bio     else []) +
        (["cognitive"]       if pid in has_cog     else []) +
        (["csf"]             if pid in has_csf     else []) +
        (["plasma"]          if pid in has_plsm    else []) +
        (["imaging"]         if pid in has_img     else []) +
        (["imaging_amyloid"] if pid in has_img_amy else []) +
        (["imaging_tau"]     if pid in has_img_tau else []) +
        (["scrna"]           if pid in has_scrna   else []) +
        (["wgs"]             if pid in has_wgs     else [])
    ))

    p_obj = {"id":pid,"diag":diag,"sex":sex,
        "age":round(age,1) if age is not None else None,
        "race":race,"ethnicity":ethnicity,"educ":None,"cohort":"ADRC","datasets":ds_list}
    participants.append(p_obj)

    # Cognitive — all columns
    if pid in has_cog:
        c = cog_by_id[pid]
        cog_cols = [k for k in c.keys() if k != "adrc_id"]
        row = {"participant_id":pid,"diagnosis":diag,
               "age":round(age,1) if age is not None else None,"sex":sex,"race":race}
        for col in cog_cols:
            row[col] = _f(c.get(col))
        cognitive.append(row)

    # Imaging — amyloid PET + tau PET
    if pid in has_img:
        row = {"participant_id":pid,"diagnosis":diag,
               "age":round(age,1) if age is not None else None,"sex":sex}
        if pid in amy_by_id:
            for col in AMY_ALL_COLS:
                key = "amy_" + col.replace("Mean.","").replace("-","_").replace(".","_")
                row[key] = _f(amy_by_id[pid].get(col))
        if pid in tau_by_id:
            for col in TAU_ALL_COLS:
                key = "tau_" + col.replace("Mean.","").replace("-","_").replace(".","_")
                row[key] = _f(tau_by_id[pid].get(col))
        imaging.append(row)

    # WGS — all SNP columns
    if pid in has_wgs:
        w = wgs_by_id[pid]
        row = {"participant_id":pid,"diagnosis":diag,"sex":sex}
        for snp in WGS_ALL_COLS:
            row[snp] = _f(w.get(snp))
        wgs_out.append(row)

    # scRNA
    if pid in has_scrna:
        s = scrna_by_id[pid]
        row = {"participant_id":pid,"diagnosis":diag,
               "age":round(age,1) if age is not None else None,"sex":sex}
        for col in SCRNA_COLS:
            row[col] = _f(s.get(col))
        scrna_out.append(row)

    # CSF proteomics
    if pid in has_csf:
        r = csf_by_id[pid]
        row = {"participant_id":pid,"diagnosis":diag,
               "age":round(age,1) if age is not None else None,"sex":sex}
        for col in CSF_PROT_COLS:
            row[col] = _f(r.get(col))
        csf_out.append(row)

    # Plasma proteomics
    if pid in has_plsm:
        r = plsm_by_id[pid]
        row = {"participant_id":pid,"diagnosis":diag,
               "age":round(age,1) if age is not None else None,"sex":sex}
        for col in PLSM_PROT_COLS:
            row[col] = _f(r.get(col))
        # Also include blood biomarkers (only cols present in biomarkers.csv)
        if pid in bio_by_id:
            b = bio_by_id[pid]
            for col in ("PTAU217","GFAP","NFL"):
                row[col] = _f(b.get(col))
        plasma_out.append(row)

    # Blood biomarkers (standalone) — only columns present in biomarkers.csv
    if pid in has_bio:
        b = bio_by_id[pid]
        row = {"participant_id":pid,"diagnosis":diag,
               "age":round(age,1) if age is not None else None,"sex":sex}
        for col in ("PTAU217","GFAP","NFL"):
            row[col] = _f(b.get(col))
        bio_out.append(row)

adrc_data = {
    "participants":  participants,
    "cognitive":    _clip_and_round(cognitive),
    "biomarkers":   _clip_and_round(bio_out),
    "imaging":      _clip_and_round(imaging),
    "wgs":          wgs_out,          # categorical genotype data — no clipping
    "scrna":        _clip_and_round(scrna_out),
    "csf":          _clip_and_round(csf_out),
    "plasma":       _clip_and_round(plasma_out),
}

payload = json.dumps(adrc_data, separators=(",",":"), ensure_ascii=False, default=str)
with open(OUT_JS, "w", encoding="utf-8") as f:
    f.write("window.ADRC_DATA=")
    f.write(payload)
    f.write(";")

size_kb = OUT_JS.stat().st_size // 1024
print(f"Wrote {OUT_JS.relative_to(ROOT)}  ({size_kb} KB)")
print(f"  participants={len(participants)}, cognitive={len(cognitive)}, "
      f"imaging={len(imaging)}, wgs={len(wgs_out)}, "
      f"csf={len(csf_out)}, plasma={len(plasma_out)}, scrna={len(scrna_out)}")

