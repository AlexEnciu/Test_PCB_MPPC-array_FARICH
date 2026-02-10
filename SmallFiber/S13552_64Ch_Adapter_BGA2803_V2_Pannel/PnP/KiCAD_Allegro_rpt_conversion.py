from pathlib import Path
import csv

SCRIPT_DIR = Path(__file__).resolve().parent

INPUT_POS  = SCRIPT_DIR / "S13552_64Ch_Adapter_BGA2803_V2_Pannel-all.pos"
OUTPUT_RPT = SCRIPT_DIR / "Allegro_PnP_from_KiCad_ALL.rpt"

ALLEGRO_FIELDS = [
    "REFDES",
    "COMP_VALUE",
    "SYM_NAME",
    "SYM_TYPE",
    "SYM_X",
    "SYM_Y",
    "SYM_CENTER_X",
    "SYM_CENTER_Y",
    "SYM_ROTATE",
    "SYM_MIRROR",
    "COMPONENT_ID",
]

def parse_kicad_pos_noheader(path: Path):
    """
    Parses KiCad .pos WITHOUT header.
    Expected columns per line (whitespace-separated):
      Ref  Val  Package  PosX  PosY  Rot  Side
    Example:
      C143 1nF C_0201_0603Metric -1.2850 -129.5921 180.0000 bottom
    """
    rows = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            parts = line.split()
            if len(parts) < 7:
                raise RuntimeError(f"Expected 7 columns but got {len(parts)} in line:\n{line}")

            ref = parts[0]
            val = parts[1]
            pkg = parts[2]
            x = float(parts[3])
            y = float(parts[4])
            rot = float(parts[5]) % 360.0
            side = parts[6].lower()

            if side not in ("top", "bottom"):
                raise RuntimeError(f"Unexpected Side='{side}' for {ref}. Expected 'top' or 'bottom'.")

            rows.append((ref, val, pkg, x, y, rot, side))

    if not rows:
        raise RuntimeError(f"No placements found in {path}")
    return rows

def kicad_to_allegro(rows):
    out = []
    for ref, val, pkg, x, y, rot, side in rows:
        out.append({
            "REFDES": ref,
            "COMP_VALUE": val,
            "SYM_NAME": pkg,
            "SYM_TYPE": "",
            "SYM_X": f"{x:.6f}",
            "SYM_Y": f"{y:.6f}",
            "SYM_CENTER_X": "",
            "SYM_CENTER_Y": "",
            "SYM_ROTATE": f"{rot:.6f}",
            "SYM_MIRROR": "YES" if side == "bottom" else "NO",
            "COMPONENT_ID": "",
        })
    return out

def write_csv(path: Path, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ALLEGRO_FIELDS)
        w.writeheader()
        w.writerows(rows)

def main():
    if not INPUT_POS.exists():
        raise FileNotFoundError(f"Input file not found: {INPUT_POS.resolve()}")

    kicad_rows = parse_kicad_pos_noheader(INPUT_POS)
    allegro_rows = kicad_to_allegro(kicad_rows)
    write_csv(OUTPUT_RPT, allegro_rows)

    print(f"OK: Read {len(kicad_rows)} placements from: {INPUT_POS.resolve()}")
    print(f"OK: Wrote Allegro-style RPT to: {OUTPUT_RPT.resolve()}")

if __name__ == "__main__":
    main()