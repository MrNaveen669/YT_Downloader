from flask import Flask, request, Response, jsonify, render_template
import subprocess
import json
import tempfile
import os
import re
from urllib.parse import unquote, quote

app = Flask(__name__)

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/api/formats", methods=["POST"])
def get_formats():
    data = request.get_json()
    url = data.get("url", "").strip()
    audio_only = data.get("audioOnly", False)

    if not url:
        return jsonify({"error": "Missing URL."}), 400

    if "list=" in url:
        return jsonify({"error": "Playlist detected. Automatic download will begin."})

    try:
        result = subprocess.run(["yt-dlp", "-j", url], capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        formats = [
            {
                "format_id": f.get("format_id"),
                "ext": f.get("ext"),
                "resolution": f.get("resolution") or f.get("height"),
                "filesize": round(f.get("filesize", 0) / 1024 / 1024, 2) if f.get("filesize") else None,
                "abr": f.get("abr")
            }
            for f in data.get("formats", [])
            if f.get("ext") in ["mp4", "m4a"] and (
                f.get("vcodec") != "none" and f.get("acodec") != "none" or f.get("acodec") != "none")
        ]
        return jsonify({"title": data.get("title", "download"), "formats": formats})
    except subprocess.CalledProcessError:
        return jsonify({"error": "Failed to fetch video data."}), 500

def sanitize_filename(filename):
    """Sanitize filename to be safe for HTTP headers and file systems"""
    # Remove or replace invalid characters
    filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
    # Remove control characters
    filename = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', filename)
    # Limit length
    filename = filename[:200]
    # Remove leading/trailing spaces and dots
    filename = filename.strip(' .')
    return filename or "download"

@app.route("/stream-download")
def stream_download():
    url = unquote(request.args.get("url", ""))
    format_id = request.args.get("format", "")
    title = unquote(request.args.get("title", "video"))
    audio_only = request.args.get("audioOnly", "false").lower() == "true"

    if "list=" in url:
        return stream_playlist(url, audio_only)

    if not format_id or not url:
        return "Missing parameters", 400

    try:
        cmd = [
            "yt-dlp",
            "-f", format_id,
            "--merge-output-format", "mp4",
            "-o", "-",
            url
        ]

        def generate():
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            try:
                for chunk in iter(lambda: process.stdout.read(4096), b""):
                    yield chunk
            finally:
                process.stdout.close()
                process.wait()

        # Determine file extension and MIME type
        is_audio = format_id in ['140', '141', '171', '249', '250', '251'] or 'm4a' in format_id
        if is_audio:
            mimetype = "audio/mpeg"
            file_ext = "mp3"
        else:
            mimetype = "video/mp4"
            file_ext = "mp4"

        # Sanitize filename
        clean_title = sanitize_filename(title)
        filename = f"{clean_title}.{file_ext}"
        
        # Create response with proper headers
        response = Response(generate(), mimetype=mimetype)
        
        # Set Content-Disposition header properly
        # Use ASCII filename with fallback for Unicode
        try:
            filename_ascii = filename.encode('ascii')
            response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
        except UnicodeEncodeError:
            # For non-ASCII filenames, use RFC 5987 format
            filename_utf8 = quote(filename.encode('utf-8'))
            response.headers['Content-Disposition'] = f"attachment; filename*=UTF-8''{filename_utf8}"
        
        # Add other headers
        response.headers['Content-Type'] = mimetype
        response.headers['Cache-Control'] = 'no-cache'
        
        return response

    except Exception as e:
        return f"Download failed: {str(e)}", 500

def stream_playlist(playlist_url, audio_only=False):
    try:
        with tempfile.TemporaryDirectory() as tmp:
            # Pre-fetch playlist video URLs
            fetch_cmd = ["yt-dlp", "--flat-playlist", "-J", playlist_url]
            meta = subprocess.run(fetch_cmd, capture_output=True, text=True, check=True)
            playlist_data = json.loads(meta.stdout)
            video_urls = [entry['url'] for entry in playlist_data.get('entries', []) if 'url' in entry]

            # Download only audio or best format with both video and audio
            for video_url in video_urls:
                try:
                    info_cmd = ["yt-dlp", "-j", f"https://www.youtube.com/watch?v={video_url}"]
                    info = subprocess.run(info_cmd, capture_output=True, text=True, check=True)
                    data = json.loads(info.stdout)

                    # Find best audio-only or best video+audio progressive format
                    chosen_format = None
                    for f in data.get("formats", []):
                        if audio_only:
                            if f.get("acodec") != "none" and f.get("vcodec") == "none":
                                chosen_format = f.get("format_id")
                                break
                        else:
                            if f.get("acodec") != "none" and f.get("vcodec") != "none":
                                chosen_format = f.get("format_id")
                                break

                    if not chosen_format:
                        continue

                    subprocess.run([
                        "yt-dlp",
                        "-f", chosen_format,
                        "--merge-output-format", "mp4",
                        "-o", os.path.join(tmp, "%(title)s.%(ext)s"),
                        f"https://www.youtube.com/watch?v={video_url}"
                    ], check=True)
                except subprocess.CalledProcessError:
                    # Skip failed videos and continue with the rest
                    continue

            from zipfile import ZipFile
            zip_path = os.path.join(tmp, "playlist.zip")
            with ZipFile(zip_path, "w") as zipf:
                for root, _, files in os.walk(tmp):
                    for file in files:
                        if file.endswith(('.mp4', '.m4a', '.webm', '.mp3')) and file != "playlist.zip":
                            zipf.write(os.path.join(root, file), arcname=file)

            def generate():
                with open(zip_path, "rb") as f:
                    while True:
                        chunk = f.read(4096)
                        if not chunk:
                            break
                        yield chunk

            response = Response(generate(), mimetype="application/zip")
            response.headers['Content-Disposition'] = 'attachment; filename="playlist.zip"'
            response.headers['Cache-Control'] = 'no-cache'
            
            return response
            
    except Exception as e:
        return f"Playlist download failed: {str(e)}", 500

if __name__ == "__main__":
    app.run(debug=True, threaded=True)