"""Encode a local Archive video to the fixed MP4 delivery profile."""
import json
import os
import subprocess
import tempfile
import math

# Only self-contained media inputs are supported (no manifests or playlists).
INPUT_OPTIONS = ["-protocol_whitelist", "file", "-format_whitelist",
                 "mov,matroska,webm,avi,asf,mpeg,mpegts,mpegvideo,flv,ogg,rm,"
                 "dv,mxf,nut,wtv,gxf,hevc,h264,m4v,mjpeg,yuv4mpegpipe,3dostr"]
VIDEO_EXTENSIONS = set(
    ".mp4 .m4v .mov .mkv .webm .avi .wmv .asf .mpeg .mpg .m2v .m1v .ts "
    ".mts .m2ts .vob .flv .ogv .3gp .3g2 .rm .rmvb .dv .mxf .mjpg "
    ".h264 .h265 .hevc .y4m .wtv .f4v .nut".split()
)


def run(args, folder):
    try:
        result = subprocess.run(args, cwd=folder, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as error:
        raise ValueError(f"Install {args[0]} and add it to PATH before saving videos.") from error
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ValueError(f"{args[0]} failed: " + (detail[-1] if detail else "invalid media or encoder"))
    return result.stdout


def probe(folder, name):
    args = ["ffprobe", "-v", "error", *INPUT_OPTIONS, "-show_streams", "-show_format", "-of", "json", name]
    return json.loads(run(args, folder))


def encode_pass(args, folder, duration, pass_number, report):
    label = f"Encoding video (pass {pass_number} of 2)"
    start = (pass_number - 1) * 50
    report(f"{label} {start}%" if duration else label)
    last = start
    with tempfile.TemporaryFile() as errors:
        try:
            proc = subprocess.Popen(args[:1] + ["-progress", "pipe:1", "-nostats"] + args[1:],
                                    cwd=folder, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=errors, text=True)
        except FileNotFoundError as error:
            raise ValueError("Install ffmpeg and add it to PATH before saving videos.") from error
        with proc:
            try:
                for line in proc.stdout:
                    key, _, value = line.strip().partition("=")
                    if duration and key == "out_time_us" and value.isdigit():
                        percent = start + min(49, int(int(value) * 50 / (duration * 1e6)))
                        if percent > last:
                            report(f"{label} {percent}%")
                            last = percent
            except BaseException:
                proc.kill()
                raise
            if proc.wait():
                errors.seek(0)
                detail = errors.read().decode("utf-8", "replace").strip().splitlines()
                raise ValueError("ffmpeg failed: " + (detail[-1] if detail else "invalid media or encoder"))


def encode_video(folder, report):
    metadata = probe(folder, "source")
    source = metadata["streams"]
    try:
        duration = float(metadata["format"].get("duration", 0))
    except (ValueError, TypeError):
        duration = 0
    if not math.isfinite(duration) or duration <= 0:
        duration = 0
    has_audio = any(s["codec_type"] == "audio" for s in source)
    # Use display aspect ratio so anamorphic and square-pixel sources both fit.
    filters = ("fps=25,scale=w='max(2,trunc(min(876,686*dar)/2)*2)':"
               "h='max(2,trunc(min(686,876/dar)/2)*2)',setsar=1,pad=876:686:(ow-iw)/2:(oh-ih)/2")
    base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-xerror",
            *INPUT_OPTIONS, "-i", "source"]
    video = ["-map", "0:V:0", "-vf", filters, "-c:v", "libx265", "-preset", "slow",
             "-pix_fmt", "yuv420p", "-b:v", "208k"]
    encode_pass(base + video + ["-x265-params", "pass=1:stats=pass.log:pools=2:log-level=error",
                        "-an", "-f", "null", os.devnull], folder, duration, 1, report)
    audio_input = [] if has_audio else ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
    audio = ["-map", "0:a:0" if has_audio else "1:a:0", "-c:a", "aac",
             "-ac", "2", "-ar", "44100", "-b:a", "126k"]
    if not has_audio:
        audio.append("-shortest")
    encode_pass(base + audio_input + video + ["-x265-params", "pass=2:stats=pass.log:pools=2:log-level=error",
                                      "-tag:v", "hvc1"] + audio + ["-movflags", "+faststart", "final.mp4"], folder, duration, 2, report)
    output = probe(folder, "final.mp4")["streams"]
    if not any(s.get("codec_name") == "hevc" and (s["width"], s["height"]) == (876, 686)
               and s["r_frame_rate"] == "25/1" for s in output) or not any(
            s.get("codec_name") == "aac" and s["channels"] == 2 and s["sample_rate"] == "44100"
            for s in output):
        raise ValueError("The encoded MP4 did not match the requested video or audio settings.")
    report("Encoding video 100%")
    return folder / "final.mp4"
