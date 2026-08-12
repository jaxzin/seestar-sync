# Temporal stacking for solar timelapses — design

**Date:** 2026-08-12 · **Status:** approved by Brian (chat) · **Scope:** `tools/solar_stabilize.py`

## Goal

Use temporal information across the stabilized timelapse to improve solar-disk detail
(sunspots, limb) while preserving the motion that makes it a timelapse — most
importantly the advancing lunar "bite" during an eclipse. Target look: **natural** —
grain-free footage with modest sharpening, no visible processing halos.

## Why it works

After stabilization the solar disk is locked to a fixed position, so sunspots are
static across the entire clip (solar rotation ≈ 0.2 px within a stacking window).
The only moving content inside the disk is the lunar limb (~0.5 px/frame). Averaging
N aligned frames reduces noise ~√N, which buys headroom to sharpen real detail.

## Design

New optional enhancement stage inside `solar_stabilize.py` pass 2 (track → warp →
**stack → sharpen** → encode). Float32 math, single encode generation — stacking a
re-encoded file would add a second compression generation, so this feeds from the
original decode.

1. **Sliding window.** Keep the last N warped/cropped frames (N odd, default 21 ≈
   ±10 frames). Output frame *j* is emitted once frame *j+N/2* is buffered; the
   window truncates at clip edges. Gate-rejected frames (clouds) are excluded from
   every window but still produce output (they stack toward themselves — passthrough).

2. **Motion-adaptive weights.** Per pixel, each window frame is weighted
   `w = exp(−d² / 2(kσ)²)` where `d` is its mean-abs-channel difference from the
   *current* frame and σ is auto-estimated frame-to-frame noise (MAD of consecutive
   warped-frame diffs in the disk core, ÷√2). Static grain (d ≈ σ) averages away;
   the moving lunar limb (d ≫ σ) collapses to the current frame — the bite stays
   crisp with full temporal resolution. Thin-cloud flicker degrades gracefully the
   same way. Implemented as a 256-entry LUT; k = 2.5.

3. **Region of interest.** Stacking applies inside a static box (~2.2 R) around the
   locked disk center; outside is passthrough (sky is measured flat zero). Keeps the
   window buffer ~80 MB and runtime ~5 min for a 678-frame 4K clip.

4. **Sharpening.** Luma-only unsharp mask (Gaussian radius ~2 px, amount 0.6) applied
   inside a feathered disk mask (full inside R+5, zero by R+25) so the halo gradient
   is never ringed. Chroma untouched — no color fringing.

5. **CLI.** `--stack [N]` (bare flag → 21) and `--sharpen AMT` (default 0.6, active
   only with `--stack`). All existing behavior unchanged when `--stack` absent; the
   verify pass still re-measures the encoded output.

## Alternatives considered

- **Separate `solar_stack.py` tool** consuming the stabilized file: cleaner
  single-purpose CLI but double compression generation, or duplicated tracking if fed
  the original. Rejected for fidelity + shared internals.
- **Geometric lunar-limb mask** (fit the moon circle from bite contour points):
  more moving parts than per-pixel motion weights, and doesn't handle clouds. Rejected.
- **Lucky-imaging quality weighting** per window frame: YAGNI for the natural look.

## Verification

- Sub-pixel disk lock unchanged on the encoded output (existing verify pass).
- Sunspot crop comparison (plain vs stacked) shows lower noise, equal-or-better detail.
- Lunar bite edge profile at mid-eclipse frames is as sharp as the unstacked version.
