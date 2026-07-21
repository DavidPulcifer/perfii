from pathlib import Path
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, jsonify, abort

from ..auth import (
    ROLE_ADMIN,
    ROLE_USER,
    can_demote_or_delete_user,
    clear_selected_user,
    complete_reset_token,
    consume_reset_token,
    create_reset_token,
    get_user,
    hash_password,
    is_admin,
    is_selected_user_authenticated,
    iso_now,
    local_reset_url,
    mark_user_authenticated,
    select_user_session,
    selected_user,
    user_has_password,
    validate_password,
    verify_password,
)
from ..db import get_meta_db, initialize_empty_db_from_template


bp = Blueprint('users', __name__, template_folder='../templates')


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _require_authenticated_selected_user():
    user = selected_user()
    if not user:
        flash("Choose a user first.", "warning")
        return redirect(url_for('users.users_home'))
    if not is_selected_user_authenticated():
        return redirect(url_for('users.login_form', user_id=int(user['id'])))
    return None


def _require_admin():
    guard = _require_authenticated_selected_user()
    if guard:
        return guard
    if not is_admin():
        flash("Admin access is required.", "warning")
        return redirect(url_for('users.users_home'))
    return None


@bp.get('/', endpoint='users_home')
def users_home():
    rows = get_meta_db().execute(
        """
        SELECT id, name, db_path, created_at, role,
               password_hash IS NOT NULL AS has_password
        FROM users ORDER BY name
        """
    ).fetchall()
    return render_template(
        'users.html',
        users=[dict(r) for r in rows],
        default_db_path=str(current_app.config['DB_PATH']),
        selected_user_id=session.get('user_id'),
        current_is_admin=is_admin() and is_selected_user_authenticated(),
    )


@bp.post('/select', endpoint='select_user')
def select_user():
    user_id = request.form.get('user_id', type=int)
    if not user_id:
        flash("Choose a user.", "warning")
        return redirect(url_for('users.users_home'))
    row = get_user(user_id)
    if not row:
        flash("User not found.", "danger")
        return redirect(url_for('users.users_home'))
    select_user_session(int(row['id']), authenticated=not user_has_password(row))
    if user_has_password(row):
        return redirect(url_for('users.login_form', user_id=int(row['id'])))
    return redirect(url_for('core.index'))


@bp.get('/login/<int:user_id>', endpoint='login_form')
def login_form(user_id: int):
    user = get_user(user_id)
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for('users.users_home'))
    if not user_has_password(user):
        select_user_session(int(user['id']), authenticated=True)
        return redirect(url_for('core.index'))
    session['user_id'] = int(user['id'])
    session.pop('auth_user_id', None)
    return render_template('users_login.html', u=dict(user))


@bp.post('/login/<int:user_id>', endpoint='login')
def login(user_id: int):
    user = get_user(user_id)
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for('users.users_home'))
    password = request.form.get('password') or ''
    if not verify_password(user['password_hash'], password):
        flash("Incorrect password.", "danger")
        return render_template('users_login.html', u=dict(user)), 403
    select_user_session(int(user['id']), authenticated=True)
    flash(f"Unlocked {user['name']}.", "success")
    return redirect(url_for('core.index'))


@bp.post('/logout', endpoint='logout')
def logout():
    clear_selected_user()
    flash("Signed out.", "success")
    return redirect(url_for('users.users_home'))


@bp.get('/settings', endpoint='settings')
def settings():
    guard = _require_authenticated_selected_user()
    if guard:
        return guard
    user = selected_user()
    return render_template('users_settings.html', u=dict(user), has_password=user_has_password(user))


@bp.post('/settings/password', endpoint='set_password')
def set_password():
    guard = _require_authenticated_selected_user()
    if guard:
        return guard
    user = selected_user()
    current_password = request.form.get('current_password') or ''
    new_password = request.form.get('new_password') or ''
    confirmation = request.form.get('confirm_password') or ''
    if user_has_password(user) and not verify_password(user['password_hash'], current_password):
        flash("Current password is incorrect.", "danger")
        return redirect(url_for('users.settings'))
    error = validate_password(new_password, confirmation)
    if error:
        flash(error, "warning")
        return redirect(url_for('users.settings'))
    meta = get_meta_db()
    meta.execute(
        "UPDATE users SET password_hash=?, password_set_at=? WHERE id=?",
        (hash_password(new_password), iso_now(), int(user['id'])),
    )
    meta.commit()
    mark_user_authenticated(int(user['id']))
    flash("Password updated.", "success")
    return redirect(url_for('users.settings'))


@bp.post('/settings/password/remove', endpoint='remove_password')
def remove_password():
    guard = _require_authenticated_selected_user()
    if guard:
        return guard
    user = selected_user()
    if user_has_password(user):
        current_password = request.form.get('current_password') or ''
        if not verify_password(user['password_hash'], current_password):
            flash("Current password is incorrect.", "danger")
            return redirect(url_for('users.settings'))
    meta = get_meta_db()
    meta.execute("UPDATE users SET password_hash=NULL, password_set_at=NULL WHERE id=?", (int(user['id']),))
    meta.commit()
    mark_user_authenticated(int(user['id']))
    flash("Password removed.", "success")
    return redirect(url_for('users.settings'))


@bp.get('/admin', endpoint='admin_users')
def admin_users():
    guard = _require_admin()
    if guard:
        return guard
    rows = get_meta_db().execute(
        """
        SELECT id, name, role, password_hash IS NOT NULL AS has_password, created_at
        FROM users ORDER BY name
        """
    ).fetchall()
    return render_template('users_admin.html', users=[dict(r) for r in rows])


@bp.post('/admin/role', endpoint='admin_set_role')
def admin_set_role():
    guard = _require_admin()
    if guard:
        return guard
    user_id = request.form.get('user_id', type=int)
    role = (request.form.get('role') or '').strip().lower()
    if role not in {ROLE_ADMIN, ROLE_USER}:
        flash("Invalid role.", "warning")
        return redirect(url_for('users.admin_users'))
    user = get_user(user_id) if user_id else None
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for('users.admin_users'))
    if user['role'] == ROLE_ADMIN and role != ROLE_ADMIN and not can_demote_or_delete_user(int(user['id'])):
        flash("Cannot demote the last Admin.", "danger")
        return redirect(url_for('users.admin_users'))
    meta = get_meta_db()
    meta.execute("UPDATE users SET role=? WHERE id=?", (role, int(user['id'])))
    meta.commit()
    flash(f"Updated {user['name']} role.", "success")
    return redirect(url_for('users.admin_users'))


@bp.post('/admin/reset', endpoint='admin_create_reset')
def admin_create_reset():
    guard = _require_admin()
    if guard:
        return guard
    user_id = request.form.get('user_id', type=int)
    user = get_user(user_id) if user_id else None
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for('users.admin_users'))
    token, expires_at = create_reset_token(int(user['id']), created_by_admin_user_id=int(selected_user()['id']))
    flash(
        f"One-time reset for {user['name']} expires at {expires_at.strftime('%Y-%m-%d %H:%M UTC')}: {local_reset_url(token)}",
        "success",
    )
    return redirect(url_for('users.admin_users'))


@bp.get('/reset/<token>', endpoint='reset_password_form')
def reset_password_form(token: str):
    token_row, error = consume_reset_token(token)
    if error:
        flash(error, "danger")
        return redirect(url_for('users.users_home'))
    user = get_user(int(token_row['user_id']))
    return render_template('users_reset.html', token=token, u=dict(user))


@bp.post('/reset/<token>', endpoint='reset_password')
def reset_password(token: str):
    token_row, error = consume_reset_token(token)
    if error:
        flash(error, "danger")
        return redirect(url_for('users.users_home'))
    new_password = request.form.get('new_password') or ''
    confirmation = request.form.get('confirm_password') or ''
    error = validate_password(new_password, confirmation)
    if error:
        user = get_user(int(token_row['user_id']))
        flash(error, "warning")
        return render_template('users_reset.html', token=token, u=dict(user)), 400
    complete_reset_token(int(token_row['id']), int(token_row['user_id']), new_password)
    select_user_session(int(token_row['user_id']), authenticated=True)
    flash("Password reset complete.", "success")
    return redirect(url_for('core.index'))


@bp.get('/new', endpoint='new_user_form')
def new_user_form():
    default_name = ""
    default_path = Path(current_app.config['USER_DB_DIR']) / "user.sqlite"
    return render_template(
        'users_new.html',
        default_name=default_name,
        default_path=str(default_path),
        desktop_mode=current_app.config.get("DESKTOP_MODE", False),
    )


@bp.post('/new', endpoint='create_user')
def create_user():
    name = (request.form.get('name') or '').strip()
    db_path_str = (request.form.get('db_path') or '').strip()
    if not name or not db_path_str:
        flash("Name and DB file path are required.", "warning")
        return redirect(url_for('users.new_user_form'))

    root = Path(current_app.config['USER_DB_DIR']).resolve()
    db_path = Path(db_path_str).expanduser()
    allow_absolute = bool(current_app.config.get("ALLOW_ABSOLUTE_USER_DB_PATHS"))

    if db_path.is_absolute():
        db_path = db_path.resolve()
        if not allow_absolute and not _is_within(db_path, root):
            flash("Production user databases must live under the configured USER_DB_DIR.", "warning")
            return redirect(url_for('users.new_user_form'))
    else:
        db_path = (root / db_path).resolve()
        if not _is_within(db_path, root):
            flash("Database path must stay inside the configured USER_DB_DIR.", "warning")
            return redirect(url_for('users.new_user_form'))

    if not _is_within(db_path, root) and not allow_absolute:
        flash("Database path must stay inside the configured USER_DB_DIR.", "warning")
        return redirect(url_for('users.new_user_form'))

    db_path = db_path.resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if not db_path.exists():
        template = Path(current_app.config['DB_PATH'])
        if not template.exists():
            flash(f"Template DB not found at {template}", "danger")
            return redirect(url_for('users.new_user_form'))
        try:
            initialize_empty_db_from_template(db_path, template)
        except Exception as ex:
            flash(f"Could not initialize empty database: {ex}", "danger")
            return redirect(url_for('users.new_user_form'))

    meta = get_meta_db()
    try:
        meta.execute(
            "INSERT INTO users(name, db_path, created_at, role) VALUES(?,?,?,?)",
            (name, str(db_path), datetime.utcnow().isoformat(timespec='seconds'), ROLE_USER)
        )
        meta.commit()
    except Exception as ex:
        flash(f"Could not create user: {ex}", "danger")
        return redirect(url_for('users.new_user_form'))

    row = meta.execute("SELECT id FROM users WHERE name=?", (name,)).fetchone()
    select_user_session(int(row['id']), authenticated=True)
    flash(f"User '{name}' created.", "success")
    return redirect(url_for('core.index'))


@bp.get('/browse-dirs')
def browse_dirs():
    root = Path(current_app.config['USER_DB_DIR']).resolve()
    rel = (request.args.get('rel') or '').strip().replace('\\', '/')
    cur = (root / rel).resolve()
    if not _is_within(cur, root):
        abort(400)
    items = []
    if cur != root:
        up_rel = str(Path(rel).parent).replace('\\', '/')
        items.append({"name": "..", "rel": up_rel, "up": True})
    for p in sorted(cur.iterdir(), key=lambda x: x.name.lower()):
        if p.is_dir():
            child_rel = str((Path(rel) / p.name)).replace('\\', '/')
            items.append({"name": p.name, "rel": child_rel})
    return jsonify({"rel": rel, "items": items})


@bp.get('/<int:user_id>/delete', endpoint='delete_user_form')
def delete_user_form(user_id: int):
    row = get_meta_db().execute(
        "SELECT id, name, db_path, created_at, role FROM users WHERE id=?",
        (user_id,)
    ).fetchone()
    if not row:
        flash("User not found.", "danger")
        return redirect(url_for('users.users_home'))
    if str(row['db_path']) == str(current_app.config['DB_PATH']) or str(row['name']).strip().lower() == 'default':
        flash("The Default user cannot be deleted.", "warning")
        return redirect(url_for('users.users_home'))
    if not can_demote_or_delete_user(int(row['id'])):
        flash("Cannot delete the last Admin.", "danger")
        return redirect(url_for('users.users_home'))
    return render_template('users_delete.html', u=dict(row))


@bp.post('/delete', endpoint='delete_user')
def delete_user():
    posted_id = request.form.get('user_id', type=int)
    confirm = (request.form.get('confirm_text') or '').strip().lower()
    if not posted_id:
        flash("Missing user id.", "warning")
        return redirect(url_for('users.users_home'))
    if confirm != "delete me":
        flash('Type "delete me" to confirm.', "warning")
        return redirect(url_for('users.delete_user_form', user_id=posted_id))

    meta = get_meta_db()
    row = meta.execute("SELECT id, name, db_path, role FROM users WHERE id=?", (posted_id,)).fetchone()
    if not row:
        flash("User not found.", "danger")
        return redirect(url_for('users.users_home'))
    if str(row['db_path']) == str(current_app.config['DB_PATH']) or str(row['name']).strip().lower() == 'default':
        flash("The Default user cannot be deleted.", "warning")
        return redirect(url_for('users.users_home'))
    if not can_demote_or_delete_user(int(row['id'])):
        flash("Cannot delete the last Admin.", "danger")
        return redirect(url_for('users.users_home'))

    db_path = Path(row['db_path'])
    if not current_app.config.get("ALLOW_ABSOLUTE_USER_DB_PATHS"):
        root = Path(current_app.config['USER_DB_DIR']).resolve()
        if not _is_within(db_path, root):
            flash("Refusing to delete a database outside the managed USER_DB_DIR.", "danger")
            return redirect(url_for('users.delete_user_form', user_id=posted_id))
    try:
        if db_path.exists():
            db_path.unlink()
    except Exception as ex:
        flash(f"Could not delete database file: {ex}", "danger")
        return redirect(url_for('users.delete_user_form', user_id=posted_id))

    try:
        meta.execute("DELETE FROM user_password_reset_tokens WHERE user_id=?", (posted_id,))
        meta.execute("DELETE FROM users WHERE id=?", (posted_id,))
        meta.commit()
    except Exception as ex:
        flash(f"Could not remove user from registry: {ex}", "danger")
        return redirect(url_for('users.users_home'))

    if session.get('user_id') == posted_id:
        clear_selected_user()
        nxt = meta.execute("SELECT id, password_hash FROM users ORDER BY id LIMIT 1").fetchone()
        if nxt:
            select_user_session(int(nxt['id']), authenticated=not bool(nxt['password_hash']))

    flash(f"User '{row['name']}' and its database were deleted.", "success")
    return redirect(url_for('users.users_home'))
