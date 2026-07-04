"""Digitize the Brelje 2018a Fig. 5 grid into refs/brelje_fig_digitized.csv.

Source image: the paper-figure crop shipped with the-hangar demo
(packages/omd/demos/brelje_2018a/figures/paper/fig5.png, itself cropped
from the published PDF -- see figures/paper/README.md there for the exact
crop command). Fig. 5 spans design range 300-800 nmi x battery specific
energy 250-800 Wh/kg; this script reads two panels:

  * "Maximum Takeoff Weight (lb)"  (bottom-right, pcolormesh,
    cells centered on grid values; the paper grid is 25-nmi-step in
    range -- our 50-step sample points land on its cell centers)
  * "Fuel mileage (lb/nmi)"        (top-left, filled contours)

Method: everything is self-calibrated from the image. Axis tick marks
(anti-aliased, so detected with a relaxed threshold) give the
pixel->data mapping; the panel's own colorbar strip gives the
color->value mapping (nearest-RGB match against the median-filtered
strip, tick values hardcoded from the published figure). No colormap
is assumed.

Validation: the paper's Table 4 publishes exact values at 500 nmi for
e_batt 250/500/750 Wh/kg (min-fuel MDO columns). Digitized cells must
land within VALIDATE_TOL of those anchors or the script fails. The
published Table 4 rows replace the pixel-derived rows in the output.

Per-cell uncertainty: the set of colorbar rows whose RGB lies within the
match tolerance of the sampled cell color spans a value interval; half
that span is reported as the 1-sigma digitization error.

Fuel values in the flat-ridge region (e_batt >= FLAT_RIDGE_EBATT) are
flagged advisory: the paper itself reports optima on flat objective
ridges there, so fuel burn is not unique even at matching objective
(Table 4 vs the-hangar reproduction differ 2x in fuel at (500 nmi,
500 Wh/kg) with <2 % objective difference).

Usage:
    python refs/tools/digitize_brelje_fig5.py <path/to/fig5.png> [out.csv]

Requires numpy + Pillow (run with the-hangar's venv).
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import numpy as np
from PIL import Image

LB_TO_KG = 0.45359237

RANGES_NM = list(range(300, 801, 50))       # 11 columns
EBATT_WHKG = list(range(250, 801, 50))      # 12 rows

# Tick label values as printed on the published figure.
MTOW_CBAR_TICKS = [8000.0, 9000.0, 10000.0, 11000.0, 12000.0]
FUEL_CBAR_TICKS = [0.0, 0.3, 0.6, 0.9, 1.2, 1.5, 1.8]
AXIS_TICKS_NM = [300.0, 400.0, 500.0, 600.0, 700.0, 800.0]
AXIS_TICKS_WHKG = [300.0, 400.0, 500.0, 600.0, 700.0, 800.0]

# Paper Table 4, "Hybrid MDO" columns (min-fuel objective, 500 nmi).
# {e_batt: (MTOW_lb, fuel_lb, mtow_at_bound)}
TABLE4_ANCHORS = {
    250: (8912.8, 754.0, False),
    500: (12566.0, 520.0, True),
    750: (12505.0, 0.0, False),
    1000: (10156.0, 0.0, False),
}
MTOW_BOUND_LB = 12566.334  # 5700 kg MTOW upper bound (active at the 500-cell)

VALIDATE_TOL = 0.03        # digitized-vs-Table-4 relative tolerance (MTOW)
FLAT_RIDGE_EBATT = 500     # fuel non-unique at/above this spec energy
FUEL_MILEAGE_FLOOR = 0.30  # lb/nmi below which fuel parity is meaningless
RGB_MATCH_TOL = 20.0       # Euclidean RGB distance counted as "same color"

SPINE_MIN_RUN = 280        # px; panel spines span the full panel edge
TICK_THRESH = 650          # RGB sum; ticks are anti-aliased, spines are not


def _max_run(mask_1d: np.ndarray) -> int:
    best = cur = 0
    for v in mask_1d:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    return best


def _spines(dark: np.ndarray, x0: int, x1: int, y0: int, y1: int,
            min_run: int = SPINE_MIN_RUN) -> tuple[int, int]:
    """Locate the left and bottom spines inside a search window.

    A spine is a row/column containing an unbroken dark run of >= min_run px
    (long enough to exclude filled-contour regions and labels). Only left +
    bottom are required: the fuel-mileage panel in the published figure has
    no top/right spines, and tick marks fully determine the calibration.
    """
    win = dark[y0:y1, x0:x1]
    cols = [i for i in range(win.shape[1]) if _max_run(win[:, i]) >= min_run]
    rows = [i for i in range(win.shape[0]) if _max_run(win[i, :]) >= min_run]
    if not cols or not rows:
        raise RuntimeError(f"axes spines not found in window ({x0},{y0})-({x1},{y1})")
    return x0 + cols[0], y0 + rows[-1]


def _cbar_box(dark: np.ndarray, shade: np.ndarray, x0: int, x1: int, y0: int, y1: int,
              min_run: int = 150) -> tuple[int, int, int, int]:
    """Colorbar strip: two tall vertical edges; top/bottom from the colored
    interior (the outline anti-aliases into bright fills, so its dark extent
    under-reports the bar)."""
    win = dark[y0:y1, x0:x1]
    cols = [i for i in range(win.shape[1]) if _max_run(win[:, i]) >= min_run]
    if len(cols) < 2:
        raise RuntimeError(f"colorbar not found in window ({x0},{y0})-({x1},{y1})")
    mid = (cols[0] + cols[-1]) // 2
    tinted = np.where(shade[y0:y1, x0 + mid] < 740)[0]
    return x0 + cols[0], x0 + cols[-1], y0 + tinted[0], y0 + tinted[-1]


def _cluster(px: list[int], gap: int = 3) -> list[float]:
    """Group adjacent pixel indices into tick centers."""
    out: list[float] = []
    group: list[int] = []
    for p in px:
        if group and p - group[-1] > gap:
            out.append(sum(group) / len(group))
            group = []
        group.append(p)
    if group:
        out.append(sum(group) / len(group))
    return out


def _tick_positions(shade: np.ndarray, *, along: str, lo: int, hi: int,
                    band_lo: int, band_hi: int, expected: int) -> list[float]:
    """Tick-mark centers just outside a spine, anti-aliasing tolerated.

    along='x': ticks below the bottom spine (band rows), returns x pixels.
    along='y': ticks left of the left spine (band cols), returns y pixels.
    """
    if along == "x":
        strip = (shade[band_lo:band_hi, lo:hi] < TICK_THRESH).all(axis=0)
    else:
        strip = (shade[lo:hi, band_lo:band_hi] < TICK_THRESH).all(axis=1)
    centers = _cluster([lo + int(i) for i in np.where(strip)[0]])
    if len(centers) != expected:
        raise RuntimeError(f"expected {expected} ticks, found {len(centers)}: {centers}")
    return centers


def _linfit(px: list[float], values: list[float]) -> tuple[float, float]:
    a, b = np.polyfit(px, values, 1)
    return float(a), float(b)


def _median_filter_rows(strip: np.ndarray, win: int = 5) -> np.ndarray:
    """Vertical median filter to erase thin divider lines in contourf colorbars."""
    pad = win // 2
    padded = np.pad(strip, ((pad, pad), (0, 0)), mode="edge")
    return np.stack([np.median(padded[i:i + win], axis=0)
                     for i in range(strip.shape[0])])


class Panel:
    """One figure panel plus its colorbar, fully pixel-calibrated."""

    def __init__(self, img: np.ndarray, dark: np.ndarray, shade: np.ndarray,
                 window: tuple[int, int, int, int],
                 cbar_window: tuple[int, int, int, int], cbar_ticks: list[float]):
        self.img = img
        self.left, self.bottom = _spines(dark, *window)
        xt = _tick_positions(shade, along="x", lo=self.left - 3, hi=window[1],
                             band_lo=self.bottom + 1, band_hi=self.bottom + 4,
                             expected=len(AXIS_TICKS_NM))
        yt = _tick_positions(shade, along="y", lo=window[2], hi=self.bottom + 4,
                             band_lo=self.left - 4, band_hi=self.left - 1,
                             expected=len(AXIS_TICKS_WHKG))
        self.ax, self.bx = _linfit(xt, AXIS_TICKS_NM)
        # y ticks run top-to-bottom while values increase upward
        self.ay, self.by = _linfit(yt, list(reversed(AXIS_TICKS_WHKG)))

        cl, cr, ct, cb = _cbar_box(dark, shade, *cbar_window)
        self.cbar_rows = np.arange(ct + 2, cb - 1)
        raw = img[self.cbar_rows, cl + 2:cr - 1, :].astype(float).mean(axis=1)
        self.cbar_rgb = _median_filter_rows(raw)
        ctick = _tick_positions(shade, along="y", lo=ct - 3, hi=cb + 4,
                                band_lo=cr + 1, band_hi=cr + 4,
                                expected=len(cbar_ticks))
        self.ac, self.bc = _linfit(ctick, list(reversed(cbar_ticks)))

    def x_px(self, range_nm: float) -> int:
        return round((range_nm - self.bx) / self.ax)

    def y_px(self, ebatt: float) -> int:
        return round((ebatt - self.by) / self.ay)

    def sample(self, range_nm: float, ebatt: float) -> tuple[float, float]:
        """(value, sigma) at a cell center via nearest-RGB colorbar match."""
        x, y = self.x_px(range_nm), self.y_px(ebatt)
        patch = self.img[y - 2:y + 3, x - 2:x + 3, :].reshape(-1, 3).astype(float)
        rgb = np.median(patch, axis=0)
        d = np.linalg.norm(self.cbar_rgb - rgb, axis=1)
        best = int(np.argmin(d))
        value = self.ac * self.cbar_rows[best] + self.bc
        close = self.cbar_rows[d <= max(d[best] + 2.0, RGB_MATCH_TOL)]
        span = (close.max() - close.min()) * abs(self.ac) if len(close) else 0.0
        return float(value), float(span / 2.0)


def main() -> int:
    fig_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else (
        Path(__file__).resolve().parents[1] / "brelje_fig_digitized.csv")

    img = np.array(Image.open(fig_path).convert("RGB"))
    if img.shape[:2] != (920, 1070):
        print(f"warning: unexpected image size {img.shape[:2]}; "
              "calibration is auto-detected but crop provenance differs")
    shade = img.sum(axis=2).astype(int)
    dk = shade < 200
    h, w = dk.shape

    mtow = Panel(img, dk, shade, (w // 2, 880, h // 2, h - 20),
                 (880, w - 60, h // 2, h - 20), MTOW_CBAR_TICKS)
    fuel = Panel(img, dk, shade, (60, 428, 20, h // 2 - 40),
                 (428, 510, 20, h // 2 - 40), FUEL_CBAR_TICKS)

    # -- validate against Table 4 anchors ------------------------------
    report = []
    for ebatt, (mtow_lb, _, _) in TABLE4_ANCHORS.items():
        if ebatt not in EBATT_WHKG:
            continue
        got, sig = mtow.sample(500, ebatt)
        rel = abs(got - mtow_lb) / mtow_lb
        report.append((ebatt, mtow_lb, got, sig, rel))
        if rel > VALIDATE_TOL:
            print(f"FAIL: (500 nmi, {ebatt} Wh/kg) digitized {got:.0f} lb "
                  f"vs Table 4 {mtow_lb} lb ({rel:.1%} > {VALIDATE_TOL:.0%})")
            return 1
    print("Table 4 validation (500 nmi):")
    for ebatt, ref, got, sig, rel in report:
        print(f"  e_batt {ebatt}: paper {ref:8.1f} lb  digitized {got:8.1f} "
              f"+/- {sig:.0f} lb  ({rel:.2%})")

    # -- emit the grid --------------------------------------------------
    rows = []
    for ebatt in EBATT_WHKG:
        for rng in RANGES_NM:
            anchor = TABLE4_ANCHORS.get(ebatt) if rng == 500 else None
            if anchor:
                mtow_lb, fuel_lb, at_bound = anchor
                mtow_sig = fuel_sig = 0.0
                source = "paper-table-4"
                mileage = fuel_lb / rng
            else:
                mtow_lb, mtow_sig = mtow.sample(rng, ebatt)
                at_bound = mtow_lb >= MTOW_BOUND_LB * (1 - 0.01)
                if at_bound:
                    mtow_lb, mtow_sig = MTOW_BOUND_LB, min(mtow_sig, 70.0)
                mileage, m_sig = fuel.sample(rng, ebatt)
                mileage = max(mileage, 0.0)
                fuel_lb, fuel_sig = mileage * rng, m_sig * rng
                source = "figure-pixel"
            fuel_check = ("strict" if ebatt < FLAT_RIDGE_EBATT and mileage >= FUEL_MILEAGE_FLOOR
                          else "advisory" if mileage >= FUEL_MILEAGE_FLOOR else "skip")
            rows.append({
                "range_nm": rng, "e_batt_whkg": ebatt,
                "mtow_kg": round(mtow_lb * LB_TO_KG, 1),
                "fuel_burn_kg": round(fuel_lb * LB_TO_KG, 1),
                "mtow_sigma_kg": round(mtow_sig * LB_TO_KG, 1),
                "fuel_sigma_kg": round(fuel_sig * LB_TO_KG, 1),
                "mtow_at_bound": int(at_bound),
                "fuel_check": fuel_check,
                "source": source,
            })

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        wtr = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        wtr.writeheader()
        wtr.writerows(rows)
    n_bound = sum(r["mtow_at_bound"] for r in rows)
    n_pub = sum(r["source"] == "paper-table-4" for r in rows)
    print(f"wrote {len(rows)} cells -> {out_path} "
          f"({n_bound} at MTOW bound, {n_pub} published)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
