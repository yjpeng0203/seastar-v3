# Batch report optimization contract

The report should let readers compare channel response, waveform quality and acquisition stability without inferring calibrated detector performance.

- Archetype: quantitative grids, with one descriptive question per figure.
- Backend: native C++/ROOT graphics (TCanvas, TH1, TH2 and TGraph). Python is used only for orchestration, data preparation and PDF auditing.
- Inputs: every stored waveform metric. All and accepted populations remain separate. Accepted means passing the existing quality mask.
- Comparison: common bin edges within each focused metric; count-density for retained full-range spectra. Focused views explicitly report below-range, above-range and missing counts.
- Evidence order: completeness and acceptance; charge and amplitude; baseline and noise; timing and shape; acquisition trends; coincidence and slow control.
- Allocation: a short overview PDF contains the primary diagnostic views; the complete diagnostic PDF retains the remaining spectra and channel views. No data or adverse observation is deleted.
- Typography: sans serif, editable PDF/SVG text, approximately 7–12 pt text, with a verified 5 pt glyph floor, bold lower-case panel labels. Final figure width 183 mm; repeated axes have equal physical plot areas.
- Statistics: N counts waveforms or readback records, not independent experimental replicates. Mean and population standard deviation remain exact; no unsupported p-values, confidence intervals, efficiency or TT/TTS claims.
- Exports: PNG, vector PDF/SVG, source-bin CSVs, a ROOT file containing focused histograms, captions, and QA records.
- QA: measured panel alignment at 1.5 pt; source preflight; exported PDF text-size and collision audits; visual inspection of all pages.
- Reuse: the existing data model is retained, but the plotting backend and layouts are replaced. Original full analysis files and the numerical kernel remain unchanged.

## Terminology ledger

| Canonical display term | Meaning | Replaces |
|---|---|---|
| Accepted waveforms | Existing quality-mask selection | Good waves / qualified / efficiency |
| Main-pulse charge (historical scale) | Area times the saved electronics conversion | Calibrated charge / NPE |
| CFD arrival time | Crossing time relative to waveform sample zero | Transit time / intrinsic TT |
| Population standard deviation | Sigma with denominator N | Error / uncertainty |
| Waveform-event rate | Rate of saved events containing waveforms | Total DAQ trigger rate |

## Text allocation

Repeated software hashes and input paths move from every page to metadata. Titles name observables; captions state population, binning and units. The overview reports acceptance and counts once. Detailed diagnostics, quality flags and full-range tails remain in the complete report. The Chinese index points to the concise report, complete report and reusable source data.

## Time-axis boundary

Every string is reported independently. No cross-string time alignment, cross-string coincidences or spatiotemporal joint analysis is performed. Within-board same-event diagnostics retain their original definition. Slow-control Beijing time is displayed separately from relative electronic time.

## Binning rule

ADC-derived quantities are binned in their native ADC or ADC.sample units before axis calibration. One native unit defines each bin. Converting coordinates must preserve the number of bins, all bin contents, underflow and overflow. Charge is a calibrated-axis clone of area; amplitude in mV is a calibrated-axis clone of amplitude in ADC. Local curves show counts per bin and full spectra show probabilities per bin, so unit conversion does not change their ordinates. Time and count quantities keep their existing native rules.

## Native preflight

The user explicitly retained the C++/ROOT graphics backend. Matplotlib-specific source checks are therefore not applied to C++ code. ROOT/Cling compilation is the source preflight, ROOT pad frames are measured in physical points, and the same backend-neutral geometry, PDF font and collision auditors inspect the final exports. No journal submission-readiness claim is made.
