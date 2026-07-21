import secrets

from flask import Flask, session, request, redirect, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from .config import Config
from .db import init_app, get_meta_db
from .blueprints.core import bp as core_bp
from .blueprints.accounts import bp as accounts_bp
from .blueprints.envelopes import bp as envelopes_bp
from .blueprints.transactions import bp as transactions_bp
from .blueprints.reconciliation import bp as reconciliation_bp
from .blueprints.imports import bp as imports_bp
from .blueprints.loans import bp as loans_bp
from .blueprints.invest import bp as invest_bp
from .blueprints.credit import bp as credit_bp
from .blueprints.bank import bp as bank_bp
from .blueprints.users import bp as users_bp
from .blueprints.savings import bp as savings_bp

from .utils import register_jinja
from .storage_monitor import get_snapshot_storage_alert

def create_app(config_object: type[Config] = Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_object)
    _validate_runtime_config(app)

    if app.config.get("TRUST_PROXY_HEADERS"):
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    # DB + Jinja
    init_app(app)
    register_jinja(app)

    # Blueprints
    app.register_blueprint(core_bp)
    app.register_blueprint(accounts_bp,   url_prefix="/accounts")
    app.register_blueprint(envelopes_bp,  url_prefix="/envelopes")
    app.register_blueprint(transactions_bp, url_prefix="/tx")
    app.register_blueprint(reconciliation_bp, url_prefix="/reconcile")
    app.register_blueprint(imports_bp,    url_prefix="/imports") 
    app.register_blueprint(loans_bp,      url_prefix="/loans") 
    app.register_blueprint(invest_bp,     url_prefix="/invest")
    app.register_blueprint(credit_bp,     url_prefix="/credit")
    app.register_blueprint(bank_bp,     url_prefix="/bank")
    app.register_blueprint(users_bp, url_prefix="/users")
    app.register_blueprint(savings_bp, url_prefix="/savings")
    
    @app.before_request
    def _ensure_user_selected():
        # Allow users/auth pages and static files without an active user.
        if request.endpoint is None:
            return
        if request.blueprint == 'users':
            return
        if request.endpoint and request.endpoint.startswith('static'):
            return

        from .auth import is_selected_user_authenticated, selected_user

        user = selected_user()
        if user is None:
            session.pop('user_id', None)
            session.pop('auth_user_id', None)
            return redirect(url_for('users.users_home'))
        if not is_selected_user_authenticated():
            return redirect(url_for('users.login_form', user_id=int(user['id'])))


    @app.context_processor
    def inject_globals():
        try:
            from .repositories import accounts_repo, envelopes_repo
            accs = accounts_repo.list_accounts()
            envs = envelopes_repo.list_envelopes()
        except Exception:
            accs, envs = [], []

        user_name = None
        current_user_role = None
        current_user_is_admin = False
        try:
            uid = session.get('user_id')
            if uid is not None:
                row = get_meta_db().execute("SELECT name, role FROM users WHERE id=?", (uid,)).fetchone()
                if row:
                    user_name = row['name']
                    current_user_role = row['role']
                    current_user_is_admin = row['role'] == 'admin'
        except Exception:
            user_name = None

        accounts_json = [
            {"id": a.get("id"), "name": a.get("name"), "account_type": a.get("account_type")}
            for a in accs
        ]

        return {
            "accounts": accs,
            "envelopes": envs,
            "accounts_map": {a["id"]: a["name"] for a in accs},
            "accounts_json": accounts_json,
            "envelopes_json": envs,  
            "balances_json": {},
            "current_user_name": user_name,
            "current_user_role": current_user_role,
            "current_user_is_admin": current_user_is_admin,
            "snapshot_storage_alert": get_snapshot_storage_alert(app),
        }

    return app


def _validate_runtime_config(app: Flask) -> None:
    env = (app.config.get("APP_ENV") or "production").lower()

    display_name = str(app.config.get("APP_DISPLAY_NAME") or "").strip()
    if not display_name or len(display_name) > 80 or not display_name.isprintable():
        raise RuntimeError("APP_DISPLAY_NAME must contain 1 to 80 printable characters.")
    app.config["APP_DISPLAY_NAME"] = display_name

    if not app.config.get("SECRET_KEY"):
        if env in {"development", "desktop", "testing"} or app.debug or app.testing:
            app.config["SECRET_KEY"] = secrets.token_urlsafe(32)
            app.logger.warning("SECRET_KEY is not set; using a temporary development key.")
        else:
            raise RuntimeError("SECRET_KEY must be set for production.")

    host = str(app.config.get("HOST") or "")
    if env == "production" and host != "127.0.0.1":
        raise RuntimeError("Production HOST must be 127.0.0.1.")
