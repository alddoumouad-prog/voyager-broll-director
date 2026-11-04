#!/usr/bin/env python3
import json, math, os, subprocess, sys, wave
import numpy as np
from PIL import Image

spec_path = sys.argv[1]
assets_dir = sys.argv[2]
out_dir = sys.argv[3]
os.makedirs(out_dir, exist_ok=True)

spec = json.load(open(spec_path, encoding="utf-8"))
input_reel = os.path.join(assets_dir, spec.get("input_file", "input_reel.mp4"))
out_slug = spec.get("output_slug", "reel_broll_master")
fps = int(spec.get("fps", 30))

dur_str = subprocess.check_output([
    "ffprobe", "-v", "error", "-show_entries", "format=duration",
    "-of", "default=noprint_wrappers=1:nokey=1", input_reel
]).decode().strip()
total_dur = float(dur_str)

# 1. Create smooth feathered mask PNG (1080x1400: solid 0..1230, smooth cosine fade 1230..1400)
mask_h = 1400
y_grid = np.arange(mask_h, dtype=np.float32)[:, None]
alpha_col = np.where(
    y_grid <= 1220,
    255.0,
    255.0 * 0.5 * (1.0 + np.cos(np.pi * (y_grid - 1220.0) / (mask_h - 1220.0)))
)
mask_arr = np.repeat(alpha_col.clip(0, 255).astype(np.uint8), 1080, axis=1)
mask_png = "/tmp/caption_safe_mask.png"
Image.fromarray(mask_arr, mode="L").save(mask_png)

# 2. Prepare each B-roll clip as RGBA ProRes4444 / qtrle or VP9-alpha / mov
brolls = spec.get("brolls", [])
prepped_brolls = []

for idx, br in enumerate(brolls):
    b_start = float(br["start"])
    b_end = min(total_dur - 0.05, float(br["end"]))
    b_dur = b_end - b_start
    if b_dur <= 0.25:
        continue

    src_file = os.path.join(assets_dir, br["file"]) if br.get("file") else ""
    if not (src_file and os.path.exists(src_file)):
        url = br["source_url"]
        src_file = f"/tmp/dl_broll_{idx}.mp4"
        if "tiktok.com" in url:
            fmt = "bytevc1_720p_468477-0/bytevc1_540p_352103-0/h264_540p_826624-0/best[format_id!=download]/best"
        else:
            fmt = "bestvideo[height<=1080][ext=mp4]/best[height<=1080]"
        subprocess.check_call(["yt-dlp", "--no-warnings", "-f", fmt, "-o", src_file, url])

    clip_ss = float(br.get("clip_start", 0.0))
    mode = br.get("mode", "caption_safe")
    prep_mov = f"/tmp/prep_br_{idx}.mov"
    fade_out_st = max(0.05, b_dur - 0.18)

    if mode == "fullscreen":
        vf = (
            f"fps={fps},scale=1134:2016:force_original_aspect_ratio=increase,"
            f"crop=1080:1920:(iw-1080)/2:(ih-1920)/2,setsar=1,"
            f"eq=contrast=1.06:saturation=1.12,"
            f"format=yuva420p,"
            f"fade=t=in:st=0:d=0.18:alpha=1,fade=t=out:st={fade_out_st:.3f}:d=0.18:alpha=1"
        )
        subprocess.check_call([
            "ffmpeg", "-y", "-stream_loop", "-1", "-ss", f"{clip_ss:.3f}",
            "-t", f"{b_dur:.3f}", "-i", src_file,
            "-vf", vf, "-an", "-c:v", "qtrle", prep_mov, "-loglevel", "error"
        ])
    else:
        # caption_safe: top 1080x1400 feathered smoothly above bottom captions!
        fc = (
            f"[0:v]fps={fps},scale=1140:1480:force_original_aspect_ratio=increase,"
            f"crop=1080:{mask_h}:(iw-1080)/2:(ih-{mask_h})/2,setsar=1,"
            f"eq=contrast=1.06:saturation=1.12,format=rgb24[fg];"
            f"[1:v]loop=loop=-1:size=1:start=0,trim=duration={b_dur:.3f},fps={fps},format=gray[msk];"
            f"[fg][msk]alphamerge,format=yuva420p,"
            f"fade=t=in:st=0:d=0.18:alpha=1,fade=t=out:st={fade_out_st:.3f}:d=0.18:alpha=1[out]"
        )
        subprocess.check_call([
            "ffmpeg", "-y", "-stream_loop", "-1", "-ss", f"{clip_ss:.3f}",
            "-t", f"{b_dur:.3f}", "-i", src_file, "-i", mask_png,
            "-filter_complex", fc, "-map", "[out]", "-an", "-c:v", "qtrle",
            prep_mov, "-loglevel", "error"
        ])

    prepped_brolls.append((b_start, b_end, prep_mov))

# 3. Build main FFmpeg filtergraph: Base + Dynamic Punch-In Jump Zooms + Feathered B-Roll Overlays
zooms = spec.get("zooms", [])
inputs = ["-i", input_reel]
for b_start, b_end, prep_mov in prepped_brolls:
    inputs.extend(["-itsoffset", f"{b_start:.3f}", "-i", prep_mov])

fc_parts = []
if zooms:
    en_expr = "+".join(f"between(t,{float(z['start']):.3f},{float(z['end']):.3f})" for z in zooms)
    fc_parts.append(
        f"[0:v]fps={fps},scale=1080:1920,setsar=1,split=2[vn][vz];"
        f"[vz]crop=964:1714:58:58,scale=1080:1920:flags=lanczos,setsar=1[vzoom];"
        f"[vn][vzoom]overlay=0:0:enable='{en_expr}'[v0]"
    )
else:
    fc_parts.append(f"[0:v]fps={fps},scale=1080:1920,setsar=1[v0]")

cur_v = "v0"
for i, (b_start, b_end, _) in enumerate(prepped_brolls):
    nxt_v = f"v{i+1}"
    inp_idx = i + 1
    fc_parts.append(
        f"[{cur_v}][{inp_idx}:v]overlay=0:0:eof_action=pass:enable='between(t,{b_start:.3f},{b_end:.3f})'[{nxt_v}]"
    )
    cur_v = nxt_v

# 4. Synthesize subtle studio air-whoosh SFX WAV at B-roll transitions if enabled
sr = 48000
sfx_wav = "/tmp/sfx_track.wav"
n_samples = int(total_dur * sr) + sr
sfx_buf = np.zeros(n_samples, dtype=np.float32)

if spec.get("sfx", True) and prepped_brolls:
    rng = np.random.default_rng(42)
    w_len = int(0.24 * sr)
    t_arr = np.linspace(0.0, 1.0, w_len, dtype=np.float32)
    env = np.sin(np.pi * t_arr) ** 2.2
    noise = rng.standard_normal(w_len).astype(np.float32)
    # Smooth moving-average low-pass for warm studio air whoosh (no harsh highs)
    kernel = np.hanning(48).astype(np.float32)
    kernel /= kernel.sum()
    whoosh = np.convolve(noise, kernel, mode="same") * env
    whoosh = whoosh / (np.max(np.abs(whoosh)) + 1e-6) * 0.065  # ~ -24 dBFS subtle!
    for b_start, _, _ in prepped_brolls:
        pos = max(0, int((b_start - 0.08) * sr))
        end_pos = min(n_samples, pos + w_len)
        sfx_buf[pos:end_pos] += whoosh[: end_pos - pos]

stereo_pcm = np.column_stack([sfx_buf[: int(total_dur * sr)], sfx_buf[: int(total_dur * sr)]])
pcm16 = (np.clip(stereo_pcm, -1.0, 1.0) * 32767.0).astype(np.int16)
with wave.open(sfx_wav, "wb") as wf:
    wf.setnchannels(2)
    wf.setsampwidth(2)
    wf.setframerate(sr)
    wf.writeframes(pcm16.tobytes())

inputs.extend(["-i", sfx_wav])
sfx_idx = len(prepped_brolls) + 1
fc_parts.append(f"[0:a][{sfx_idx}:a]amix=inputs=2:duration=first:dropout_transition=0,volume=2.0[aout]")

stage_mkv = "/tmp/stage_broll.mkv"
subprocess.check_call([
    "ffmpeg", "-y", *inputs,
    "-filter_complex", ";".join(fc_parts),
    "-map", f"[{cur_v}]", "-map", "[aout]",
    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "14", "-pix_fmt", "yuv420p", "-r", str(fps),
    "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
    stage_mkv, "-loglevel", "error"
])

# 5. Final Voyager Broadcast Mastering (-14 LUFS, -1 dBTP, H.264 High social-9x16)
proj = "/tmp/broll_proj.vvideo"
subprocess.check_call(["rm", "-rf", proj])
subprocess.check_call(["voyager-video", "new", proj, stage_mkv])

out_mp4 = os.path.join(out_dir, f"{out_slug}.mp4")
out_preview = os.path.join(out_dir, f"{out_slug}_preview.png")
out_sheet = os.path.join(out_dir, f"{out_slug}_sheet.jpg")

subprocess.check_call([
    "voyager-video", "-C", proj, "render",
    "--preset", "social-9x16",
    "--loudness", "-14LUFS",
    "--true-peak", "-1dBTP",
    "-o", out_mp4
])

prev_t = float(spec.get("preview_sec", prepped_brolls[0][0] + 1.0 if prepped_brolls else 3.0))
subprocess.check_call([
    "ffmpeg", "-y", "-ss", f"{prev_t:.2f}", "-i", out_mp4,
    "-frames:v", "1", out_preview, "-loglevel", "error"
])
# Generate 6-panel contact sheet hitting exact B-roll & zoom moments
sample_ts = [float(b[0]) + 1.2 for b in prepped_brolls[:4]]
while len(sample_ts) < 4:
    sample_ts.append(min(total_dur - 1.0, 2.0 + len(sample_ts) * 6.0))
panel_imgs = []
for k, ts in enumerate(sample_ts[:4]):
    p_img = f"/tmp/p_{k}.png"
    subprocess.check_call([
        "ffmpeg", "-y", "-ss", f"{min(total_dur - 0.5, ts):.2f}", "-i", out_mp4,
        "-vf", "scale=360:640", "-frames:v", "1", p_img, "-loglevel", "error"
    ])
    panel_imgs.append(Image.open(p_img))
sheet_im = Image.new("RGB", (360 * len(panel_imgs), 640))
for k, pim in enumerate(panel_imgs):
    sheet_im.paste(pim, (k * 360, 0))
sheet_im.save(out_sheet, quality=92)
print("Successfully rendered B-Roll Master:", out_mp4)
