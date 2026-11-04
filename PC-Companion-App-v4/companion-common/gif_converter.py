#!/usr/bin/env python3
"""Convert an animated GIF into a PixelClock .pca animation.

The device plays custom animations from its flash filesystem as the
"Custom animation" ambient style. This tool does the heavy lifting on the
PC so the firmware stays trivial: frames are fitted to the 128x64 panel,
quantized to one global palette of up to 16 colors and packed at 4 bits
per pixel.

PCA1 format (little-endian):
    offset      size    field
    0           4       magic "PCA1"
    4           2       frameCount N (1..360)
    6           2       defaultFrameMs (metadata, 20..5000)
    8           1       paletteLen P (2..16)
    9           1       flags (bit0: loop)
    10          2       reserved (0)
    12          P*2     palette, RGB565
    12+P*2      N*2     per-frame delay in ms (clamped 10..5000)
    12+P*2+N*2  N*4096  4bpp frames, 2 px/byte, high nibble = left pixel

Usage:
    python tools/gif2pca.py input.gif
    python tools/gif2pca.py input.gif -o fine.pca --anchor end --preview check.gif
    python tools/gif2pca.py input.gif --upload http://pixelclock.local

Requires Pillow (pip install pillow). Upload uses the standard library.
"""

import argparse
import io
import os
import struct
import sys

from PIL import Image

PANEL_W, PANEL_H = 128, 64


def pixel_data(img):
    """Flat pixel sequence; Pillow 14 renames getdata()."""
    if hasattr(img, "get_flattened_data"):
        return list(img.get_flattened_data())
    return list(img.getdata())
MAX_FRAMES = 360
MIN_DELAY_MS = 10  # GIF timing is stored in centiseconds; output is sampled at 30 Hz
MAX_DELAY_MS = 5000
MAX_PCA_BYTES = 1536 * 1024  # device hard cap


def load_frames(path):
    """Return ([RGB frames], [delays ms]) with GIF disposal handled."""
    im = Image.open(path)
    frames, delays = [], []
    index = 0
    try:
        while True:
            im.seek(index)
            frames.append(im.convert("RGB"))
            delays.append(int(im.info.get("duration", 100)) or 100)
            index += 1
    except EOFError:
        pass
    if not frames:
        sys.exit("no frames found in %s" % path)
    return frames, delays


def fit_frame(frame, mode, anchor):
    """Fit a frame using the same nearest-neighbor mapping as Mica's HUB75 formatter."""
    w, h = frame.size
    if mode == "stretch":
        draw_w, draw_h = PANEL_W, PANEL_H
        offset_x = offset_y = 0.0
    else:
        width_scale = PANEL_W / float(w)
        height_scale = PANEL_H / float(h)
        scale = max(width_scale, height_scale) if mode == "crop" else min(width_scale, height_scale)
        # Match MathF.Round's midpoint-away-from-zero behavior in Mica.
        draw_w = max(1, int(w * scale + 0.5))
        draw_h = max(1, int(h * scale + 0.5))
        offset_x = (PANEL_W - draw_w) / 2.0
        offset_y = (PANEL_H - draw_h) / 2.0

        # Preserve the Companion's crop anchor while using the Mica pixel map.
        if mode == "crop" and anchor == "start":
            offset_x = 0.0 if draw_w > PANEL_W else offset_x
            offset_y = 0.0 if draw_h > PANEL_H else offset_y
        elif mode == "crop" and anchor == "end":
            offset_x = PANEL_W - draw_w if draw_w > PANEL_W else offset_x
            offset_y = PANEL_H - draw_h if draw_h > PANEL_H else offset_y

    source = frame.convert("RGB")
    source_pixels = source.load()
    output = Image.new("RGB", (PANEL_W, PANEL_H), (0, 0, 0))
    output_pixels = output.load()

    for target_y in range(PANEL_H):
        local_y = target_y - offset_y
        if local_y < 0.0 or local_y >= draw_h:
            continue
        source_y = min(h - 1, max(0, int(local_y * h / draw_h)))
        for target_x in range(PANEL_W):
            local_x = target_x - offset_x
            if local_x < 0.0 or local_x >= draw_w:
                continue
            source_x = min(w - 1, max(0, int(local_x * w / draw_w)))
            output_pixels[target_x, target_y] = source_pixels[source_x, source_y]
    return output


def quantize_frames(frames, colors):
    """One global palette across all frames; returns (index frames, palette565)."""
    stack = Image.new("RGB", (PANEL_W, PANEL_H * len(frames)))
    for k, f in enumerate(frames):
        stack.paste(f, (0, PANEL_H * k))
    pal_img = stack.quantize(colors=colors, method=Image.FASTOCTREE)
    quantized = [f.quantize(palette=pal_img, dither=Image.Dither.NONE) for f in frames]

    used = max(max(pixel_data(f)) for f in quantized) + 1
    rgb = pal_img.getpalette()[: used * 3]
    palette565 = []
    for c in range(used):
        r, g, b = rgb[c * 3], rgb[c * 3 + 1], rgb[c * 3 + 2]
        palette565.append(((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3))
    return quantized, palette565


def pack_frame(indexed):
    data = pixel_data(indexed)
    packed = bytearray(PANEL_W * PANEL_H // 2)
    for p in range(0, len(data), 2):
        packed[p // 2] = (data[p] << 4) | data[p + 1]
    return bytes(packed)


def build_pca(quantized, palette565, delays):
    n = len(quantized)
    p = max(2, len(palette565))
    default_ms = max(20, min(MAX_DELAY_MS, round(sum(delays) / n)))
    out = io.BytesIO()
    out.write(b"PCA1")
    out.write(struct.pack("<HHBBH", n, default_ms, p, 1, 0))
    for c in range(p):
        out.write(struct.pack("<H", palette565[c] if c < len(palette565) else 0))
    for d in delays:
        out.write(struct.pack("<H", d))
    for f in quantized:
        out.write(pack_frame(f))
    return out.getvalue()


def pack_rgb565(frame):
    """Encode a fitted RGB frame as little-endian RGB565 pixels."""
    raw = bytearray()
    rgb = frame.convert("RGB")
    pixels = rgb.get_flattened_data() if hasattr(rgb, "get_flattened_data") else rgb.getdata()
    for r, g, b in pixels:
        value = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        raw.extend((value & 0xff, value >> 8))
    return bytes(raw)


def build_pca2(frames, delays):
    """PCA2 stores each 128x64 frame directly as 16-bit RGB565."""
    count = len(frames)
    default_ms = max(20, min(MAX_DELAY_MS, round(sum(delays) / count)))
    out = io.BytesIO()
    out.write(b"PCA2")
    out.write(struct.pack("<HHBBH", count, default_ms, 0, 1, 0))
    for delay in delays:
        out.write(struct.pack("<H", delay))
    for frame in frames:
        out.write(pack_rgb565(frame))
    return out.getvalue()


def write_preview(quantized, delays, path):
    frames = [f.convert("RGB").resize((PANEL_W * 4, PANEL_H * 4), Image.NEAREST)
              for f in quantized]
    frames[0].save(path, save_all=True, append_images=frames[1:],
                   duration=delays, loop=0)


def upload(blob, name, base_url):
    import urllib.request
    boundary = "----gif2pca"
    body = io.BytesIO()
    body.write(("--%s\r\nContent-Disposition: form-data; name=\"anim\"; "
                "filename=\"%s.pca\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n"
                % (boundary, name)).encode())
    body.write(blob)
    body.write(("\r\n--%s--\r\n" % boundary).encode())
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/anim/upload?name=" + name,
        data=body.getvalue(),
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(req, timeout=60) as resp:
        print("device:", resp.read().decode(errors="replace"))


def sanitize_name(raw):
    name = "".join(c for c in raw if c.isascii() and (c.isalnum() or c in "_-"))[:24]
    return name or "anim"


def main():
    ap = argparse.ArgumentParser(description="Convert a GIF to a PixelClock .pca animation")
    ap.add_argument("gif", help="input GIF file")
    ap.add_argument("-o", "--output", help="output .pca path (default: <gif>.pca)")
    ap.add_argument("--fit", choices=["crop", "pad", "stretch"], default="crop",
                    help="how to fit to 128x64 (default crop)")
    ap.add_argument("--anchor", choices=["center", "start", "end"], default="center",
                    help="crop anchor: start=top/left edge kept, end=bottom/right "
                         "(e.g. --anchor end keeps a bottom caption)")
    ap.add_argument("--colors", type=int, default=16, choices=range(2, 17),
                    metavar="2-16", help="palette size (default 16)")
    ap.add_argument("--frame-skip", type=int, default=1, metavar="N",
                    help="keep every Nth frame, delays merged (default 1 = all)")
    ap.add_argument("--preview", metavar="OUT.GIF",
                    help="also write an upscaled preview GIF of the converted result")
    ap.add_argument("--upload", metavar="URL",
                    help="POST the result to the device, e.g. http://pixelclock.local")
    args = ap.parse_args()

    frames, delays = load_frames(args.gif)
    if args.frame_skip > 1:
        merged = []
        for i in range(0, len(frames), args.frame_skip):
            merged.append(sum(delays[i:i + args.frame_skip]))
        frames = frames[::args.frame_skip]
        delays = merged
    if len(frames) > MAX_FRAMES:
        sys.exit("too many frames (%d > %d); use --frame-skip" % (len(frames), MAX_FRAMES))
    delays = [max(MIN_DELAY_MS, min(MAX_DELAY_MS, d)) for d in delays]

    fitted = [fit_frame(f, args.fit, args.anchor) for f in frames]
    quantized, palette565 = quantize_frames(fitted, args.colors)
    blob = build_pca(quantized, palette565, delays)
    if len(blob) > MAX_PCA_BYTES:
        sys.exit("result is %d KiB, over the device cap of %d KiB; use --frame-skip"
                 % (len(blob) // 1024, MAX_PCA_BYTES // 1024))

    out_path = args.output or os.path.splitext(args.gif)[0] + ".pca"
    with open(out_path, "wb") as fh:
        fh.write(blob)
    total_ms = sum(delays)
    print("%s: %d frames, %d colors, %.1fs loop, %d KiB -> %s"
          % (os.path.basename(args.gif), len(quantized), len(palette565),
             total_ms / 1000.0, len(blob) // 1024, out_path))

    if args.preview:
        write_preview(quantized, delays, args.preview)
        print("preview:", args.preview)

    if args.upload:
        name = sanitize_name(os.path.splitext(os.path.basename(out_path))[0])
        upload(blob, name, args.upload)
    else:
        print("upload with: curl -F \"anim=@%s\" \"http://pixelclock.local/api/anim/upload\""
              % out_path)


if __name__ == "__main__":
    main()
