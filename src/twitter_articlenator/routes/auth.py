"""Login, logout, and administrator account routes."""

from __future__ import annotations

from urllib.parse import urlsplit

import click
from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from ..auth import SESSION_AUTH_VERSION_KEY, SESSION_USER_KEY, current_user, get_user_store
from ..security import is_valid_csrf_request

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    """Authenticate a local account."""
    if current_user() is not None:
        return redirect(url_for("pages.index"))
    if request.method == "GET":
        return render_template("login.html")
    if not is_valid_csrf_request():
        return render_template("login.html", error="Invalid or expired form token"), 400

    username = request.form.get("username", "")
    password = request.form.get("password", "")
    rate_limiter = current_app.extensions["login_rate_limiter"]
    attempt_key = request.remote_addr or "unknown"
    if not rate_limiter.is_allowed(attempt_key):
        return render_template("login.html", error="Too many login attempts; try again later"), 429
    user = get_user_store().authenticate(username, password)
    if user is None:
        rate_limiter.record_failure(attempt_key)
        return render_template("login.html", error="Invalid username or password"), 401

    next_target = _safe_next(request.args.get("next"))
    session.clear()
    session[SESSION_USER_KEY] = user.id
    session[SESSION_AUTH_VERSION_KEY] = user.auth_version
    session.permanent = True
    return redirect(next_target)


@auth_bp.route("/logout", methods=["POST"])
def logout():
    """End the current login session."""
    if not is_valid_csrf_request():
        return {"error": "Invalid or missing CSRF token"}, 400
    session.clear()
    return redirect(url_for("auth.login"))


@auth_bp.route("/admin/users", methods=["GET", "POST"])
def users_admin():
    """Create and list local accounts."""
    user = current_user()
    if user is None or not user.is_admin:
        abort(403)
    store = get_user_store()
    if request.method == "POST":
        if not is_valid_csrf_request():
            return {"error": "Invalid or missing CSRF token"}, 400
        try:
            store.create_user(
                request.form.get("username", ""),
                request.form.get("password", ""),
                is_admin=request.form.get("is_admin") == "on",
            )
        except ValueError as exc:
            return render_template(
                "admin_users.html", users=store.list_users(), error=str(exc)
            ), 400
        flash("Account created")
        return redirect(url_for("auth.users_admin"))
    return render_template("admin_users.html", users=store.list_users())


def register_cli(app) -> None:
    """Register administrator-facing account commands."""

    @app.cli.group("users")
    def users_group():
        """Manage local Articlenator accounts."""

    @users_group.command("create")
    @click.option("--username", prompt=True)
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    @click.option("--admin", is_flag=True, help="Grant account-management access.")
    def create_user_command(username: str, password: str, admin: bool):
        """Create a local account."""
        try:
            user = app.extensions["user_store"].create_user(
                username,
                password,
                is_admin=admin,
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"Created {'admin ' if user.is_admin else ''}user {user.username}")

    @users_group.command("set-password")
    @click.option("--username", prompt=True)
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    def set_password_command(username: str, password: str):
        """Replace an account password."""
        try:
            user = app.extensions["user_store"].set_password(username, password)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"Updated password for {user.username}")

    def set_account_active(username: str, *, active: bool) -> None:
        try:
            user = app.extensions["user_store"].set_active(username, active=active)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"{'Enabled' if active else 'Disabled'} user {user.username}")

    @users_group.command("disable")
    @click.option("--username", prompt=True)
    def disable_user_command(username: str):
        """Revoke an account's access."""
        set_account_active(username, active=False)

    @users_group.command("enable")
    @click.option("--username", prompt=True)
    def enable_user_command(username: str):
        """Restore an account's access."""
        set_account_active(username, active=True)


def _safe_next(candidate: str | None) -> str:
    if not candidate:
        return url_for("pages.index")
    parsed = urlsplit(candidate)
    if (
        parsed.scheme
        or parsed.netloc
        or not parsed.path.startswith("/")
        or parsed.path.startswith("//")
    ):
        return url_for("pages.index")
    return candidate
