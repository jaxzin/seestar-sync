#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "opencv-python-headless>=4.9",
#   "numpy>=1.26",
#   "matplotlib>=3.8",
# ]
# ///
"""Stabilize a solar (eclipse) timelapse by locking the solar disk to a fixed spot.

Built for Seestar solar timelapses: a bright disk on a near-black sky.
Eclipse-safe: the disk center comes from a robust circle fit to the solar
limb (threshold -> largest contour -> convex hull -> RANSAC circle ->
fixed-radius refinement), not from a brightness centroid, so the lunar
"bite" does not pull the tracked center off the true solar center.

Pipeline:
  pass 1  detect the solar-disk center in every frame
  pass 2  translate each frame (sub-pixel) so the disk sits at the median
          center, crop off the shift margins, pipe to ffmpeg (H.264)
  verify  re-detect on the output and report residual jitter
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


# ---------------------------------------------------------------- video I/O


@dataclass
class VideoInfo:
    width: int
    height: int
    fps: float
    nb_frames: int


def probe(path: Path) -> VideoInfo:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate,nb_frames",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, check=True,
    ).stdout
    s = json.loads(out)["streams"][0]
    num, den = s["r_frame_rate"].split("/")
    return VideoInfo(
        width=int(s["width"]),
        height=int(s["height"]),
        fps=float(num) / float(den),
        nb_frames=int(s.get("nb_frames", 0)),
    )


def iter_frames(path: Path, info: VideoInfo):
    """Yield BGR uint8 frames decoded by the system ffmpeg."""
    frame_size = info.width * info.height * 3
    proc = subprocess.Popen(
        [
            "ffmpeg", "-v", "error", "-i", str(path),
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
        ],
        stdout=subprocess.PIPE,
        bufsize=frame_size,
    )
    try:
        while True:
            buf = proc.stdout.read(frame_size)
            if len(buf) < frame_size:
                break
            yield np.frombuffer(buf, np.uint8).reshape(info.height, info.width, 3)
    finally:
        proc.stdout.close()
        proc.wait()


def open_encoder(path: Path, width: int, height: int, fps: float, crf: int) -> subprocess.Popen:
    return subprocess.Popen(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{width}x{height}", "-r", f"{fps}", "-i", "-",
            "-c:v", "libx264", "-preset", "slow", "-crf", str(crf),
            "-vf", "scale=out_color_matrix=bt709,format=yuv420p",
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
            "-movflags", "+faststart",
            str(path),
        ],
        stdin=subprocess.PIPE,
    )


# ---------------------------------------------------------- disk detection


def circle_from_3(p: np.ndarray):
    (ax, ay), (bx, by), (cx, cy) = p
    d = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return None
    a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    ux = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d
    uy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d
    return ux, uy, float(np.hypot(ax - ux, ay - uy))


def kasa_fit(pts: np.ndarray):
    """Algebraic least-squares circle fit."""
    x, y = pts[:, 0], pts[:, 1]
    A = np.column_stack([x, y, np.ones_like(x)])
    b = x * x + y * y
    sol, *_ = np.linalg.lstsq(A, b, rcond=None)
    cx, cy = sol[0] / 2.0, sol[1] / 2.0
    r = float(np.sqrt(max(sol[2] + cx * cx + cy * cy, 0.0)))
    return float(cx), float(cy), r


def ransac_circle(pts: np.ndarray, rng: np.random.Generator,
                  tol: float, iters: int, r_bounds: tuple[float, float]):
    best_inliers = None
    n = len(pts)
    for _ in range(iters):
        sample = pts[rng.choice(n, 3, replace=False)]
        model = circle_from_3(sample)
        if model is None or not (r_bounds[0] <= model[2] <= r_bounds[1]):
            continue
        cx, cy, r = model
        d = np.abs(np.hypot(pts[:, 0] - cx, pts[:, 1] - cy) - r)
        inliers = d < tol
        if best_inliers is None or inliers.sum() > best_inliers.sum():
            best_inliers = inliers
    if best_inliers is None or best_inliers.sum() < 6:
        return None
    # Two refit rounds on inliers tighten the model.
    for _ in range(2):
        cx, cy, r = kasa_fit(pts[best_inliers])
        d = np.abs(np.hypot(pts[:, 0] - cx, pts[:, 1] - cy) - r)
        best_inliers = d < tol
        if best_inliers.sum() < 6:
            return None
    # Fit-quality metrics: residual RMS and the angular arc the inliers span.
    rms = float(np.sqrt(np.mean(d[best_inliers] ** 2)))
    ang = np.degrees(np.arctan2(pts[best_inliers, 1] - cy, pts[best_inliers, 0] - cx))
    arc = 10.0 * len(np.unique((ang // 10).astype(int)))
    return cx, cy, r, rms, arc


def refine_center_fixed_r(pts: np.ndarray, cx: float, cy: float, radius: float,
                          tol: float = 3.0, iters: int = 25):
    """Gauss-Newton refit of the center with the radius held fixed.

    With a deep eclipse bite the visible limb arc shrinks, and a free-radius
    fit trades radius error against center error. Locking the radius to the
    global median removes that wobble.
    """
    c = np.array([cx, cy], float)
    for _ in range(iters):
        d = pts - c
        rho = np.hypot(d[:, 0], d[:, 1])
        mask = np.abs(rho - radius) < tol
        if mask.sum() < 6:
            return float(c[0]), float(c[1])
        u = d[mask] / rho[mask, None]
        resid = rho[mask] - radius
        step, *_ = np.linalg.lstsq(u, resid, rcond=None)
        c += step
        if np.hypot(*step) < 1e-4:
            break
    return float(c[0]), float(c[1])


def limb_hull(frame_bgr: np.ndarray, min_radius: float):
    """Threshold the disk and return the convex hull of its outline."""
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    peak = float(np.percentile(blur, 99.9))
    if peak < 30:  # no disk (clouds, dropout)
        return None
    thr = max(20.0, 0.4 * peak)
    _, binimg = cv2.threshold(blur, thr, 255, cv2.THRESH_BINARY)
    contours, _ = cv2.findContours(binimg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return None
    cnt = max(contours, key=cv2.contourArea)
    if cv2.contourArea(cnt) < 0.05 * np.pi * min_radius ** 2:
        return None
    hull = cv2.convexHull(cnt).reshape(-1, 2).astype(np.float64)
    return hull if len(hull) >= 8 else None


def detect_all(path: Path, info: VideoInfo, label: str):
    """Pass over the video, returning per-frame (cx, cy, r) and hull points."""
    r_bounds = (60.0, 0.48 * min(info.width, info.height))
    rng = np.random.default_rng(20260812)
    centers, hulls, quals = [], [], []
    for i, frame in enumerate(iter_frames(path, info)):
        hull = limb_hull(frame, r_bounds[0])
        fit = ransac_circle(hull, rng, tol=2.5, iters=400, r_bounds=r_bounds) if hull is not None else None
        if fit is None:
            centers.append((np.nan, np.nan, np.nan))
            hulls.append(None)
            quals.append((np.nan, np.nan))
        else:
            cx, cy, r, rms, arc = fit
            centers.append((cx, cy, r))
            hulls.append(hull)
            quals.append((rms, arc))
        if i % 100 == 0:
            print(f"  [{label}] frame {i}: "
                  + ("no fit" if fit is None else f"center=({cx:.1f},{cy:.1f}) r={r:.1f}"),
                  flush=True)
    return np.array(centers), hulls, np.array(quals)


# ------------------------------------------------------------------- main


def fill_nan_runs(track: np.ndarray) -> np.ndarray:
    """Linearly interpolate NaN entries (isolated detection failures)."""
    out = track.copy()
    idx = np.arange(len(track))
    for col in range(out.shape[1]):
        good = ~np.isnan(out[:, col])
        if good.sum() < len(track):
            out[~good, col] = np.interp(idx[~good], idx[good], out[good, col])
    return out


def clean_track(centers: np.ndarray, quals: np.ndarray,
                r_tol: float = 10.0, rms_tol: float = 1.5, arc_min: float = 120.0):
    """Reject untrustworthy fits and interpolate through them.

    Gates on fit *quality*, not trajectory smoothness, so genuine
    single-frame mount jumps survive while cloud frames do not:
      - radius must match the global median (the sun cannot resize),
      - limb residual RMS must be sub-1.5px (clouds blur the limb),
      - inliers must span a wide arc (cloud blobs fit short arcs).

    Returns (cleaned track, valid mask).
    """
    c = centers.copy()
    r_med = float(np.nanmedian(c[:, 2]))
    bad = (np.isnan(c[:, 0])
           | (np.abs(c[:, 2] - r_med) > r_tol)
           | (quals[:, 0] > rms_tol)
           | (quals[:, 1] < arc_min))
    c[bad] = np.nan
    return fill_nan_runs(c), ~bad


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("input", type=Path)
    ap.add_argument("-o", "--output", type=Path, default=None,
                    help="stabilized video (default: <input>-stabilized.mp4)")
    ap.add_argument("--crf", type=int, default=16, help="x264 quality (default 16)")
    ap.add_argument("--compare", type=Path, default=None,
                    help="also write a side-by-side original/stabilized crop video")
    ap.add_argument("--report", type=Path, default=None,
                    help="write a PNG plot of the center track before/after")
    args = ap.parse_args()

    inp: Path = args.input
    out: Path = args.output or inp.with_name(inp.stem + "-stabilized.mp4")
    info = probe(inp)
    print(f"input: {info.width}x{info.height} @ {info.fps:g} fps, ~{info.nb_frames} frames", flush=True)

    # -- pass 1: track the disk --------------------------------------------
    raw, hulls, quals = detect_all(inp, info, "track")
    n = len(raw)

    r_med = float(np.nanmedian(raw[:, 2]))
    for i, hull in enumerate(hulls):  # radius-locked refinement
        if hull is not None:
            raw[i, 0], raw[i, 1] = refine_center_fixed_r(
                hull, raw[i, 0], raw[i, 1], r_med)

    centers, valid = clean_track(raw, quals)
    n_interp = int((~valid).sum())
    if n_interp:
        print(f"note: {n_interp}/{n} frames rejected (clouds/no fit); shifts interpolated there", flush=True)
        if n_interp > n // 4:
            print("error: too many unusable frames; aborting", file=sys.stderr)
            return 1

    tx, ty = np.median(centers[valid, 0]), np.median(centers[valid, 1])
    dx, dy = tx - centers[:, 0], ty - centers[:, 1]

    # -- crop that removes the black translation margins -------------------
    ml = max(0, int(np.ceil(dx.max())));  mr = max(0, int(np.ceil(-dx.min())))
    mt = max(0, int(np.ceil(dy.max())));  mb = max(0, int(np.ceil(-dy.min())))
    x0, y0 = ml, mt
    w = (info.width - ml - mr) // 2 * 2
    h = (info.height - mt - mb) // 2 * 2

    print(f"disk radius ~{r_med:.1f}px, locking center to ({tx:.1f},{ty:.1f})", flush=True)
    print(f"shift range: x [{dx.min():+.1f},{dx.max():+.1f}]  y [{dy.min():+.1f},{dy.max():+.1f}] px", flush=True)
    print(f"output: {w}x{h} (cropped from {info.width}x{info.height})", flush=True)

    # -- pass 2: warp + encode ---------------------------------------------
    enc = open_encoder(out, w, h, info.fps, args.crf)
    for i, frame in enumerate(iter_frames(inp, info)):
        m = np.float32([[1, 0, dx[i]], [0, 1, dy[i]]])
        warped = cv2.warpAffine(frame, m, (info.width, info.height),
                                flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT)
        enc.stdin.write(warped[y0:y0 + h, x0:x0 + w].tobytes())
        if i % 100 == 0:
            print(f"  [encode] frame {i}/{n}", flush=True)
    enc.stdin.close()
    if enc.wait() != 0:
        print("error: ffmpeg encode failed", file=sys.stderr)
        return 1

    # -- verify: re-detect on the stabilized output ------------------------
    out_info = probe(out)
    post_raw, _, post_quals = detect_all(out, out_info, "verify")
    post, post_valid = clean_track(post_raw, post_quals)

    def jitter(track, mask):
        gx, gy = track[mask, 0], track[mask, 1]
        return (np.std(gx), np.std(gy), np.ptp(gx), np.ptp(gy))

    bx, by, bpx, bpy = jitter(centers, valid)
    ax_, ay_, apx, apy = jitter(post, post_valid)
    print("\ndisk-center jitter (std / peak-to-peak, px):")
    print(f"  before: x {bx:6.2f} / {bpx:6.2f}   y {by:6.2f} / {bpy:6.2f}")
    print(f"  after:  x {ax_:6.2f} / {apx:6.2f}   y {ay_:6.2f} / {apy:6.2f}")
    print(f"wrote {out}", flush=True)

    if args.report:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, axes = plt.subplots(2, 1, figsize=(10, 6), sharex=True)
        for k, (axp, name) in enumerate(zip(axes, ("x", "y"))):
            axp.plot(centers[:, k] - np.mean(centers[valid, k]), label=f"before (std {jitter(centers, valid)[k]:.2f}px)")
            axp.plot(post[:, k] - np.mean(post[post_valid, k]), label=f"after (std {jitter(post, post_valid)[k]:.2f}px)")
            axp.set_ylabel(f"{name}-center offset (px)")
            axp.legend(loc="upper right")
            axp.grid(True, alpha=0.3)
        axes[1].set_xlabel("frame")
        fig.suptitle("Solar-disk center track, before vs after stabilization")
        fig.tight_layout()
        fig.savefig(args.report, dpi=110)
        print(f"wrote {args.report}", flush=True)

    if args.compare:
        # Side-by-side crops centered on the (fixed) target center.
        box = int(min(2 * np.ceil(r_med * 1.35), h, w))
        box -= box % 4  # side-by-side halves the height; keep both even
        def crop_at(img, cx_, cy_):
            x = int(round(cx_ - box / 2)); y = int(round(cy_ - box / 2))
            x = max(0, min(img.shape[1] - box, x)); y = max(0, min(img.shape[0] - box, y))
            return img[y:y + box, x:x + box]
        sbs_w, sbs_h = box, box // 2
        cmp_enc = open_encoder(args.compare, sbs_w, sbs_h, info.fps, 18)
        for i, (orig, stab) in enumerate(zip(iter_frames(inp, info), iter_frames(out, out_info))):
            left = crop_at(orig, tx, ty)
            right = crop_at(stab, tx - x0, ty - y0)
            sbs = cv2.resize(np.hstack([left, right]), (sbs_w, sbs_h), interpolation=cv2.INTER_AREA)
            for txt, xoff in (("original", 10), ("stabilized", sbs_w // 2 + 10)):
                cv2.putText(sbs, txt, (xoff, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2, cv2.LINE_AA)
            cmp_enc.stdin.write(sbs.tobytes())
        cmp_enc.stdin.close()
        cmp_enc.wait()
        print(f"wrote {args.compare}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
