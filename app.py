#!/usr/bin/env python3
"""
APK Converter Backend — Production-ready for Render
Converts XAPK and APKM files to standard APK.

Improvements:
- Health check endpoint for uptime monitoring
- File size limit (200MB)
- Auto-cleanup of old upload/output files
"""

import os
import sys
import json
import zipfile
import tempfile
import shutil
import time
from pathlib import Path
from flask import Flask, request, send_file, jsonify, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename

app = Flask(__name__, static_folder=".")
CORS(app)

UPLOAD_FOLDER = Path("uploads")
OUTPUT_FOLDER = Path("outputs")
UPLOAD_FOLDER.mkdir(exist_ok=True)
OUTPUT_FOLDER.mkdir(exist_ok=True)

ALLOWED_EXTENSIONS = {"xapk", "apkm"}
MAX_FILE_SIZE = 200 * 1024 * 1024  # 200 MB
CLEANUP_MAX_AGE_HOURS = 1

print(f"[STARTUP] APK Converter starting on port {os.environ.get('PORT', '5000')}", file=sys.stderr)
print(f"[STARTUP] CWD: {os.getcwd()}", file=sys.stderr)
print(f"[STARTUP] Files in CWD: {os.listdir('.')}", file=sys.stderr)


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def cleanup_old_files(folder: Path, max_age_hours: int = CLEANUP_MAX_AGE_HOURS):
    """Delete files older than max_age_hours to prevent disk bloat."""
    now = time.time()
    cutoff = max_age_hours * 3600
    deleted = 0
    for f in folder.iterdir():
        if f.is_file() and (now - f.stat().st_mtime) > cutoff:
            try:
                f.unlink()
                deleted += 1
            except OSError:
                pass
    if deleted:
        print(f"[CLEANUP] Deleted {deleted} old file(s) from {folder}", file=sys.stderr)


def convert_xapk(xapk_path: Path, out_dir: Path) -> dict:
    results = {
        "success": False,
        "apk_path": None,
        "obb_paths": [],
        "manifest": None,
        "message": "",
        "splits": []
    }

    with zipfile.ZipFile(xapk_path, "r") as zf:
        namelist = zf.namelist()

        manifest_names = [n for n in namelist if n.lower().endswith("manifest.json")]
        if manifest_names:
            try:
                results["manifest"] = json.loads(zf.read(manifest_names[0]))
            except Exception:
                pass

        apk_candidates = [n for n in namelist if n.lower().endswith(".apk")]
        base_apk = None
        for cand in apk_candidates:
            if "base" in cand.lower() or "split_" not in cand.lower().split("/")[-1]:
                base_apk = cand
                break
        if not base_apk and apk_candidates:
            base_apk = apk_candidates[0]

        if not base_apk:
            results["message"] = "No APK found inside the XAPK archive."
            return results

        apk_name = Path(base_apk).name
        out_apk = out_dir / apk_name
        with zf.open(base_apk) as src, open(out_apk, "wb") as dst:
            shutil.copyfileobj(src, dst)
        results["apk_path"] = out_apk

        for name in namelist:
            if ".obb" in name.lower():
                out_obb = out_dir / Path(name).name
                with zf.open(name) as src, open(out_obb, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                results["obb_paths"].append(out_obb)

        splits = [n for n in namelist if n.lower().endswith(".apk") and n != base_apk]
        results["splits"] = [Path(s).name for s in splits]

        results["success"] = True
        results["message"] = f"Extracted {apk_name}"
        if results["obb_paths"]:
            results["message"] += f" + {len(results['obb_paths'])} OBB file(s)"
        if splits:
            results["message"] += f" + {len(splits)} split APK(s)"

    return results


def convert_apkm(apkm_path: Path, out_dir: Path) -> dict:
    results = {
        "success": False,
        "apk_path": None,
        "manifest": None,
        "message": "",
        "splits": []
    }

    try:
        with zipfile.ZipFile(apkm_path, "r") as zf:
            namelist = zf.namelist()

            manifest_names = [n for n in namelist if n.lower().endswith("manifest.json")]
            if manifest_names:
                try:
                    results["manifest"] = json.loads(zf.read(manifest_names[0]))
                except Exception:
                    pass

            apk_files = [n for n in namelist if n.lower().endswith(".apk")]
            base_apk = None
            for cand in apk_files:
                if "base" in cand.lower():
                    base_apk = cand
                    break
            if not base_apk and apk_files:
                base_apk = apk_files[0]

            if base_apk:
                apk_name = Path(base_apk).name
                out_apk = out_dir / apk_name
                with zf.open(base_apk) as src, open(out_apk, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                results["apk_path"] = out_apk
                results["success"] = True
                results["message"] = f"Extracted {apk_name} from APKM"

                splits = [n for n in apk_files if n != base_apk]
                results["splits"] = [Path(s).name for s in splits]
                if splits:
                    results["message"] += f" + {len(splits)} split APK(s)"
            else:
                results["message"] = "No APK found inside the APKM archive."

    except zipfile.BadZipFile:
        results["message"] = (
            "This APKM file appears to be encrypted (APKMirror proprietary format). "
            "Encrypted APKM files cannot be decrypted by this tool. "
            "Use the official APKMirror Installer app or UnApkm (F-Droid) instead."
        )
    except Exception as e:
        results["message"] = f"Error reading APKM: {str(e)}"

    return results


@app.before_request
def before_request():
    """Run cleanup and size check before every request."""
    cleanup_old_files(UPLOAD_FOLDER)
    cleanup_old_files(OUTPUT_FOLDER)

    if request.content_length and request.content_length > MAX_FILE_SIZE:
        return jsonify({"error": "File too large. Max 200MB."}), 413


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/health")
def health():
    """Health check endpoint for uptime monitors (e.g. UptimeRobot)."""
    return jsonify({"status": "ok", "service": "apk-converter"}), 200


@app.route("/api/convert", methods=["POST"])
def api_convert():
    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No selected file"}), 400

    if not allowed_file(file.filename):
        return jsonify({"error": "Invalid file type. Only .xapk and .apkm are supported."}), 400

    ext = file.filename.rsplit(".", 1)[1].lower()
    filename = secure_filename(file.filename)
    tmp_path = UPLOAD_FOLDER / filename
    file.save(tmp_path)

    with tempfile.TemporaryDirectory() as tmpdir:
        out_dir = Path(tmpdir)

        if ext == "xapk":
            result = convert_xapk(tmp_path, out_dir)
        else:
            result = convert_apkm(tmp_path, out_dir)

        if not result["success"]:
            return jsonify({"error": result["message"]}), 400

        package_name = filename.rsplit(".", 1)[0] + "_converted.zip"
        package_path = OUTPUT_FOLDER / package_name

        with zipfile.ZipFile(package_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(result["apk_path"], arcname=result["apk_path"].name)
            for obb in result.get("obb_paths", []):
                zf.write(obb, arcname=f"obb/{obb.name}")
            info = {
                "original": filename,
                "base_apk": result["apk_path"].name,
                "obb_files": [p.name for p in result.get("obb_paths", [])],
                "split_apks": result.get("splits", []),
                "manifest": result.get("manifest"),
                "message": result["message"]
            }
            zf.writestr("info.json", json.dumps(info, indent=2))

        return send_file(
            package_path,
            mimetype="application/zip",
            as_attachment=True,
            download_name=package_name
        )


@app.route("/api/info", methods=["POST"])
def api_info():
    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No selected file"}), 400

    ext = file.filename.rsplit(".", 1)[1].lower()
    filename = secure_filename(file.filename)
    tmp_path = UPLOAD_FOLDER / filename
    file.save(tmp_path)

    try:
        with zipfile.ZipFile(tmp_path, "r") as zf:
            namelist = zf.namelist()
            apks = [n for n in namelist if n.lower().endswith(".apk")]
            obbs = [n for n in namelist if ".obb" in n.lower()]
            manifests = [n for n in namelist if n.lower().endswith("manifest.json")]
            manifest_data = None
            if manifests:
                try:
                    manifest_data = json.loads(zf.read(manifests[0]))
                except Exception:
                    pass

        return jsonify({
            "filename": filename,
            "format": ext.upper(),
            "total_files": len(namelist),
            "apk_files": apks,
            "obb_files": obbs,
            "manifest": manifest_data,
            "is_plain_zip": True
        })
    except zipfile.BadZipFile:
        return jsonify({
            "filename": filename,
            "format": ext.upper(),
            "is_plain_zip": False,
            "note": "This file is not a plain ZIP. It may be an encrypted APKM."
        })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
