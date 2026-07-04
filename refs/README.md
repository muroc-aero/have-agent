# Reference data

## brelje_fig_digitized.csv

MTOW and fuel-burn reference grid for the Brelje replication study
(spec §5 `acceptance.parity.reference`), 132 cells over design range
300-800 nmi x battery specific energy 250-800 Wh/kg.

Source: Brelje, B. J. and Martins, J. R. R. A., "Development of a
Conceptual Design Model for Aircraft Electric Propulsion with Efficient
Gradients," AIAA/IEEE EATS 2018 (AIAA 2018-4979).
https://websites.umich.edu/~mdolaboratory/pdf/Brelje2018a.pdf

Two provenance classes per row (`source` column):

- `paper-table-4` (3 rows): exact published values from the paper's
  Table 4 "Hybrid MDO" columns (min-fuel objective, 500 nmi design
  range, e_batt 250/500/750 Wh/kg). Read directly from the paper PDF,
  page 21. Note the King Air MTOW upper bound (12566 lb / 5700 kg) is
  active at the 500 Wh/kg cell.
- `figure-pixel` (129 rows): digitized from the paper's Fig. 5
  "Maximum Takeoff Weight (lb)" pcolormesh and "Fuel mileage (lb/nmi)"
  contour panels by `tools/digitize_brelje_fig5.py` -- pixel-color
  inversion against each panel's own colorbar, axis calibration from
  tick marks. Validated against the Table 4 anchors to within 0.12 %
  (see script output). `*_sigma_kg` columns carry the per-cell
  digitization uncertainty (half the value span of colorbar rows
  indistinguishable from the sampled color).

Columns:

- `range_nm`, `e_batt_whkg` -- case coordinates (grid step 50/50)
- `mtow_kg`, `fuel_burn_kg` -- reference values (paper works in lb;
  converted at 0.45359237 kg/lb)
- `mtow_sigma_kg`, `fuel_sigma_kg` -- 1-sigma digitization uncertainty
  (0 for published rows)
- `mtow_at_bound` -- 1 where the optimizer sits on the 5700 kg MTOW
  upper bound (65 cells; digitized values are clamped to the bound)
- `fuel_check` -- `strict` | `advisory` | `skip`. Fuel burn is only a
  meaningful parity target where the min-fuel optimum is unique.
  At/above 500 Wh/kg the paper's own optima sit on flat objective
  ridges (its Table 4 500-cell burns 520 lb where the-hangar's
  reproduction burns 218 lb at <2 % objective difference), so fuel
  mismatches there are advisory (warn), not failures. `skip` marks
  near-all-electric cells (mileage < 0.3 lb/nmi) where relative fuel
  tolerance is meaningless.

Regenerate with:

```bash
# from the-hangar checkout (needs numpy + Pillow)
uv run --with pillow python <have-agent>/refs/tools/digitize_brelje_fig5.py \
    packages/omd/demos/brelje_2018a/figures/paper/fig5.png \
    <have-agent>/refs/brelje_fig_digitized.csv
```

Cross-check against the-hangar's independent 11x12 reproduction
(`packages/omd/demos/brelje_2018a/results/fig5_grid.csv`): 118/132
cells agree with this reference within the 3 % MTOW parity tolerance;
the misses cluster on the staircase boundaries of the at-bound region,
where the optimizer legitimately lands in different basins run-to-run.
Those cells are expected review/triage candidates, not digitization
errors.
