"""Page routes blueprint."""

from flask import Blueprint, jsonify, render_template, send_from_directory
from werkzeug.utils import secure_filename

from ..config import get_config

pages_bp = Blueprint("pages", __name__)


@pages_bp.route("/")
def index():
    """GET / - Main UI with link input."""
    return render_template("index.html")


@pages_bp.route("/setup")
def setup():
    """GET /setup - Cookie extraction guide."""
    return render_template("cookie_guide.html")


@pages_bp.route("/download/<filename>")
def download(filename: str):
    """GET /download/<filename> - Download generated PDF."""
    # Security: sanitize filename using werkzeug
    safe_filename = secure_filename(filename)

    # Security: only allow PDF files
    if not safe_filename.endswith(".pdf"):
        return jsonify({"error": "Only PDF files can be downloaded"}), 400

    # Security: ensure filename wasn't completely sanitized away
    if not safe_filename or safe_filename != filename:
        return jsonify({"error": "Invalid filename"}), 400

    config = get_config()
    output_dir = config.output_dir

    pdf_path = output_dir / safe_filename
    if not pdf_path.exists():
        return jsonify({"error": "File not found"}), 404

    return send_from_directory(
        output_dir, safe_filename, mimetype="application/pdf", as_attachment=True
    )
