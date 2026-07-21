from flask import current_app, flash, redirect, request, url_for

from ..repositories.import_review_drafts_repo import discard_import_review_draft
from ..services.import_commit_service import (
    ImportSourceUnavailableError,
    load_import_commit_account,
    perform_import_commit,
)
from ..services.import_rule_proposal_service import safe_refresh_import_rule_proposals
from ..services.import_undo_service import undo_import_session


def register_import_commit_routes(
    bp,
    *,
    get_account_func,
    update_account_func,
    list_fitids_func,
    get_transaction_func,
    edit_transaction_func,
    create_transfer_func,
    create_expense_func,
    create_income_func,
    record_import_provenance_func=None,
    get_import_review_source_func=None,
    get_import_session_undo_candidate_func=None,
    latest_import_session_id_for_account_func=None,
    delete_import_session_provenance_func=None,
    delete_transaction_func=None,
) -> None:
    @bp.post('/commit')
    def commit_import():
        posted = request.form
        account_selection = load_import_commit_account(posted, get_account_func)
        if not account_selection.ok:
            flash(account_selection.error_message, account_selection.flash_category)
            return redirect(url_for('imports.upload_form'))

        try:
            tally, _matched_ids = perform_import_commit(
                account_id=account_selection.account_id,
                account=account_selection.account,
                form=posted,
                list_fitids_func=list_fitids_func,
                update_account_func=update_account_func,
                get_transaction_func=get_transaction_func,
                edit_transaction_func=edit_transaction_func,
                get_account_func=get_account_func,
                create_transfer_func=create_transfer_func,
                create_expense_func=create_expense_func,
                create_income_func=create_income_func,
                logger=current_app.logger,
                flash_func=flash,
                record_import_provenance_func=record_import_provenance_func,
                get_import_review_source_func=get_import_review_source_func,
            )
        except ImportSourceUnavailableError as ex:
            flash(str(ex), "warning")
            return redirect(url_for('imports.upload_form'))
        draft_fingerprint = (posted.get("import_draft_fingerprint") or "").strip()
        if draft_fingerprint and tally.skipped == 0:
            discard_import_review_draft(draft_fingerprint, account_selection.account_id)
        safe_refresh_import_rule_proposals(
            account_id=account_selection.account_id,
            reason="import_commit",
            logger=current_app.logger,
        )
        return redirect(url_for('transactions.list_'))

    @bp.post('/undo-last')
    def undo_last_import():
        if not (
            get_import_session_undo_candidate_func
            and latest_import_session_id_for_account_func
            and delete_import_session_provenance_func
            and delete_transaction_func
        ):
            flash("Import undo is not available.", "warning")
            return redirect(url_for('transactions.list_'))

        try:
            session_id = request.form.get("import_session_id", type=int)
            result = undo_import_session(
                session_id=session_id,
                get_import_session_undo_candidate_func=get_import_session_undo_candidate_func,
                latest_import_session_id_for_account_func=latest_import_session_id_for_account_func,
                delete_transaction_func=delete_transaction_func,
                delete_import_session_provenance_func=delete_import_session_provenance_func,
            )
            flash(result.message, result.category)
        except ValueError as ex:
            flash(str(ex), "warning")
        except Exception as ex:
            current_app.logger.exception("IMPORT UNDO: failed: %s", ex)
            flash("Import undo failed.", "danger")
        return redirect(url_for('transactions.list_'))
