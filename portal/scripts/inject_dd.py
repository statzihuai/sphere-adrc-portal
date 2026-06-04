#!/usr/bin/env python3
"""
scripts/inject_dd.py
Build data/adrc_dd.js from DD CSV files in data/dd/.

Includes ALL data variables (not just the portal-highlighted subset):
  - cognitive:  all 80 neuropsychological battery items
  - imaging:    all 232 amyloid PET + 228 tau PET + 72 thickness regions
  - wgs:        all 167 AD GWAS SNPs
  - scrna:      all 996 gene × cell-type expression columns
  - csf:        all 5,284 SomaScan CSF aptamers
  - plasma:     all 6,900 SomaScan plasma aptamers + 7 blood biomarkers

Usage (from project root):
    python3 scripts/inject_dd.py
"""
import csv, json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DD   = ROOT / "data" / "dd"
OUT  = ROOT / "data" / "adrc_dd.js"

SKIP = frozenset({"adrc_id","pidn","year_quarter","visit_number","type",
                   "c2_completed","b4_completed","additional_race"})

def read_dd(path):
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def compact(d):
    """Remove None/empty-string values from a dict."""
    return {k: v for k, v in d.items() if v not in (None, "", "nan")}

dd = {}

# ── Cognitive ──────────────────────────────────────────────────────────────
rows = read_dd(DD / "DD_cognitive_c2_b4.csv")
dd["cognitive"] = []
for r in rows:
    col = r.get("column","")
    if not col or col in SKIP:
        continue
    dd["cognitive"].append(compact({
        "col":   col,
        "label": r.get("description",""),
        "desc":  f"{r.get('Form','')} — {r.get('Section','')}".strip(" —"),
        "type":  r.get("type",""),
        "range": r.get("Valid_Range_Options",""),
    }))

# ── Imaging — amyloid PET ──────────────────────────────────────────────────
rows = read_dd(DD / "DD_amyloid_intensities_variable_summary.csv")
dd["imaging"] = []
for r in rows:
    col = r.get("column","")
    if not col or col in SKIP:
        continue
    # portal output name: "amy_" + col without "Mean." prefix, dots/dashes → _
    out_col = "amy_" + col.replace("Mean.","").replace("-","_").replace(".","_") if col.startswith("Mean.") else col
    dd["imaging"].append(compact({
        "col":    out_col,
        "label":  r.get("description",""),
        "desc":   f"Amyloid PET — region: {col.replace('Mean.','')}",
        "type":   "numeric",
        "source": "Amyloid PET (FreeSurfer parcellation)",
        "orig":   col,
    }))

# ── Imaging — tau PET ──────────────────────────────────────────────────────
rows = read_dd(DD / "DD_tau_intensities_variable_summary.csv")
for r in rows:
    col = r.get("column","")
    if not col or col in SKIP:
        continue
    out_col = "tau_" + col.replace("Mean.","").replace("-","_").replace(".","_") if col.startswith("Mean.") else col
    dd["imaging"].append(compact({
        "col":    out_col,
        "label":  r.get("description",""),
        "desc":   f"Tau PET — region: {col.replace('Mean.','')}",
        "type":   "numeric",
        "source": "Tau PET (FreeSurfer parcellation)",
        "orig":   col,
    }))

# ── Imaging — cortical thickness ───────────────────────────────────────────
rows = read_dd(DD / "DD_thickness_variable_summary.csv")
seen_thick = set()
for r in rows:
    col = r.get("column","")
    if not col or col in SKIP:
        continue
    # bilateral average name: strip lh_/rh_ prefix, keep base name
    base = col.replace("lh_","").replace("rh_","")
    if base in seen_thick:
        continue
    seen_thick.add(base)
    dd["imaging"].append(compact({
        "col":    base,
        "label":  r.get("description","").replace("left hemisphere ","").replace("right hemisphere ",""),
        "desc":   f"FreeSurfer cortical thickness — {base} (bilateral average of lh + rh)",
        "type":   "numeric",
        "source": "Structural MRI (FreeSurfer bilateral average)",
        "unit":   "mm",
    }))

# ── WGS ────────────────────────────────────────────────────────────────────
rows = read_dd(DD / "DD_wgs_genodata.csv")
dd["wgs"] = []
for r in rows:
    col = r.get("column","")
    if not col or col in SKIP:
        continue
    dd["wgs"].append(compact({
        "col":   col,
        "label": r.get("description",""),
        "desc":  f"{r.get('Gene','')} · {r.get('cytoband','')} · REF={r.get('REF','')} ALT={r.get('ALT','')}".strip(" ·"),
        "type":  "categorical",
        "gene":  r.get("Gene",""),
        "chr":   r.get("CHROMhg38",""),
        "pos":   r.get("POShg38",""),
        "source":"WGS AD GWAS panel",
    }))

# ── scRNA ──────────────────────────────────────────────────────────────────
rows = read_dd(DD / "DD_sc_rna_seq_cellexp.csv")
dd["scrna"] = []
for r in rows:
    col = r.get("column","")
    if not col or col in SKIP:
        continue
    dd["scrna"].append(compact({
        "col":   col,
        "label": r.get("description",""),
        "type":  "numeric",
        "source":"PBMC scRNA-seq (SPHERE)",
    }))

# ── CSF Proteomics ─────────────────────────────────────────────────────────
rows = read_dd(DD / "DD_proteomics_somalogic_csf.csv")
dd["csf"] = []
for r in rows:
    col = r.get("column","")
    if not col or col in SKIP:
        continue
    dd["csf"].append(compact({
        "col":      col,
        "label":    r.get("description",""),
        "desc":     r.get("TargetFullName",""),
        "type":     "numeric",
        "gene":     r.get("EntrezGeneSymbol",""),
        "uniprot":  r.get("UniProt",""),
        "soma_id":  r.get("SomaId",""),
        "source":   "CSF SomaScan (Somalogic)",
    }))

# ── Plasma Proteomics + blood biomarkers ───────────────────────────────────
rows = read_dd(DD / "DD_proteomics_somalogic_plasma.csv")
dd["plasma"] = []
for r in rows:
    col = r.get("column","")
    if not col or col in SKIP:
        continue
    dd["plasma"].append(compact({
        "col":     col,
        "label":   r.get("description",""),
        "desc":    r.get("TargetFullName",""),
        "type":    "numeric",
        "gene":    r.get("EntrezGeneSymbol",""),
        "uniprot": r.get("UniProt",""),
        "soma_id": r.get("SomaId",""),
        "source":  "Plasma SomaScan (Somalogic)",
    }))

bio_rows = read_dd(DD / "DD_biomarkers.csv")
for r in bio_rows:
    col = r.get("column","")
    if not col or col in SKIP:
        continue
    dd["plasma"].append(compact({
        "col":    col,
        "label":  r.get("description",""),
        "type":   "numeric",
        "source": "Blood biomarker (targeted assay)",
    }))

# ── Write ──────────────────────────────────────────────────────────────────
payload = json.dumps(dd, separators=(",",":"), ensure_ascii=False)
with open(OUT, "w", encoding="utf-8") as f:
    f.write("window.ADRC_DD=")
    f.write(payload)
    f.write(";")

size_kb = OUT.stat().st_size // 1024
print(f"Wrote {OUT.relative_to(ROOT)}  ({size_kb} KB)")
for ds, entries in dd.items():
    print(f"  {ds}: {len(entries)} variables")
