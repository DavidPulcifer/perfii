(function(){
  const READY = (fn) =>
    document.readyState === 'loading'
      ? document.addEventListener('DOMContentLoaded', fn, { once: true })
      : fn();

  READY(() => {
    // -------- Utilities --------
    const money = (c) => {
      const n = (parseInt(c,10)||0)/100;
      const s = n < 0 ? '-' : '';
      const a = Math.abs(n).toFixed(2);
      return s + '$' + a;
    };
    const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (ch) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[ch]));
    const accountSel = document.getElementById('importAccount');
    const reviewForm = document.getElementById('importReviewForm');
    if (!reviewForm) return;

    const manualCandidatesUrl = reviewForm.dataset.manualCandidatesUrl || '';
    const dupesUrl = reviewForm.dataset.dupesUrl || '';
    const draftSaveUrl = reviewForm.dataset.draftSaveUrl || '';
    const draftDiscardUrl = reviewForm.dataset.draftDiscardUrl || '';
    const lazyFields = document.getElementById('importReviewLazyFields');
    const envelopes = readJson('envelopesJSON', []);
    const accounts = readJson('ACCOUNTS_JSON', []);
    const importPrefills = readJson('IMPORT_PREFILLS_JSON', []);
    const importPayeePrefills = readJson('IMPORT_PAYEE_PREFILLS_JSON', []);
    const importRowStates = readJson('IMPORT_ROW_STATES_JSON', []);
    const importDraftIdentity = readJson('IMPORT_DRAFT_IDENTITY_JSON', {});
    const importReviewDraft = readJson('IMPORT_REVIEW_DRAFT_JSON', null);
    const initialPrefillAccountId = String(document.getElementById('importAccount')?.value || '');
    const draftScopeAccountId = String(importDraftIdentity.account_id || initialPrefillAccountId || '');
    const draftFingerprintInput = reviewForm.querySelector('input[name="import_draft_fingerprint"]');
    let lastAccountId = initialPrefillAccountId;
    const prefilledRows = new Set();
    const predictionFeedbackRows = new Map();
    const payeeNormalizedRows = new Set();
    const memoNormalizedRows = new Set();
    const rowStore = new Map();

    function initRowStore() {
      (importRowStates || []).forEach((state) => {
        const idx = String(state.row_index);
        rowStore.set(idx, {
          rowIndex: Number(state.row_index),
          section: state.section || 'zero',
          amountCents: parseInt(state.amount_cents, 10) || 0,
          fitid: String(state.fitid || ''),
          draftRowFingerprint: String(state.draft_row_fingerprint || ''),
          initialAlreadyImported: !!state.already_imported,
          duplicate: !!state.already_imported,
          matched: false,
        });
      });
    }

    function storedRow(rowIndex) {
      const idx = String(rowIndex);
      if (!rowStore.has(idx)) {
        const tr = rowFor(idx);
        rowStore.set(idx, {
          rowIndex: Number(rowIndex),
          section: tr?.dataset.section || 'zero',
          amountCents: parseInt(tr?.dataset.amountCents || '0', 10) || 0,
          fitid: String(tr?.dataset.fitid || ''),
          draftRowFingerprint: '',
          initialAlreadyImported: false,
          duplicate: false,
          matched: false,
        });
      }
      return rowStore.get(idx);
    }

    function rowIsResolved(rowIndex) {
      const state = storedRow(rowIndex);
      return !!(state.initialAlreadyImported || state.duplicate || state.matched);
    }

    function setRowDuplicateState(rowIndex, duplicate) {
      const state = storedRow(rowIndex);
      state.duplicate = !!(state.initialAlreadyImported || duplicate);
    }

    initRowStore();
    const lazyState = new Map(); // rowIndex -> { fieldName: value }
    let activeLazy = null;       // { type: 'split'|'transfer', rowIndex }
    let modalDoneRowToCheck = null;

    function readJson(id, fallback) {
      const el = document.getElementById(id);
      if (!el) return fallback;
      try { return JSON.parse(el.textContent || '[]'); } catch { return fallback; }
    }

    function accountUrl(baseUrl, accountId, extraParams) {
      const url = new URL(baseUrl, window.location.origin);
      url.searchParams.set('account_id', accountId);
      Object.entries(extraParams || {}).forEach(([key, value]) => {
        if (value !== undefined && value !== null && value !== '') {
          url.searchParams.set(key, value);
        }
      });
      return url.toString();
    }

    function rowFor(rowIndex) {
      return document.querySelector(`tr[data-row-index="${rowIndex}"]`);
    }

    function rowLabel(tr) {
      const postedAt = tr?.dataset.postedAt || '';
      const payee = tr?.dataset.payee || '';
      return `${postedAt}${payee ? ' · ' + payee : ''}`;
    }

    const importSortState = new Map();

    function sortableSectionRows(section) {
      return Array.from(document.querySelectorAll(`table[data-import-sort-section="${CSS.escape(section)}"] tbody tr[data-section="${CSS.escape(section)}"]`));
    }

    function normalizeSortText(value) {
      return String(value || '').trim().toLocaleLowerCase();
    }

    function currentPayeeValue(tr) {
      const rowIndex = tr?.dataset.rowIndex || '';
      return tr?.querySelector(`input[name="payee_${CSS.escape(rowIndex)}"]`)?.value || tr?.dataset.payee || '';
    }

    function originalSourceValue(tr) {
      const rowIndex = tr?.dataset.rowIndex || '';
      const originalPayee = tr?.querySelector(`input[name="orig_payee_${CSS.escape(rowIndex)}"]`)?.value || tr?.dataset.payee || '';
      const originalMemo = tr?.querySelector(`input[name="orig_memo_${CSS.escape(rowIndex)}"]`)?.value || '';
      return originalPayeeTooltipText(originalPayee, originalMemo) || originalPayee || originalMemo;
    }

    function originalRowOrder(tr) {
      return parseInt(tr?.dataset.originalOrder || tr?.dataset.rowIndex || '0', 10) || 0;
    }

    function compareImportRows(a, b, key, direction) {
      const multiplier = direction === 'desc' ? -1 : 1;
      let cmp = 0;
      if (key === 'payee') {
        cmp = normalizeSortText(currentPayeeValue(a)).localeCompare(normalizeSortText(currentPayeeValue(b)));
      } else if (key === 'source') {
        cmp = normalizeSortText(originalSourceValue(a)).localeCompare(normalizeSortText(originalSourceValue(b)));
      } else {
        cmp = String(a.dataset.postedAt || '').localeCompare(String(b.dataset.postedAt || ''));
      }
      if (cmp === 0) cmp = originalRowOrder(a) - originalRowOrder(b);
      return cmp * multiplier;
    }

    function updateImportSortHeaderState(section, key, direction) {
      document.querySelectorAll(`.import-sort-header[data-import-sort-section="${CSS.escape(section)}"]`).forEach((btn) => {
        const active = btn.dataset.importSort === key;
        btn.setAttribute('aria-pressed', active ? 'true' : 'false');
        btn.setAttribute('aria-sort', active ? (direction === 'desc' ? 'descending' : 'ascending') : 'none');
      });
    }

    function sortImportSection(section, key) {
      persistModal(splitModal);
      persistModal(transferModal);
      const prior = importSortState.get(section) || {};
      const direction = prior.key === key && prior.direction === 'asc' ? 'desc' : 'asc';
      importSortState.set(section, { key, direction });
      const rows = sortableSectionRows(section);
      if (!rows.length) return;
      const tbody = rows[0].parentElement;
      rows.sort((a, b) => compareImportRows(a, b, key, direction)).forEach((row) => tbody.appendChild(row));
      updateImportSortHeaderState(section, key, direction);
    }

    function dollarsFromCents(cents) {
      return (Math.abs(parseInt(cents, 10) || 0) / 100).toFixed(2);
    }

    function rowState(rowIndex) {
      return lazyState.get(String(rowIndex)) || {};
    }

    function renderHiddenFields() {
      if (!lazyFields) return;
      lazyFields.innerHTML = '';
      const frag = document.createDocumentFragment();
      Array.from(lazyState.keys()).sort((a, b) => Number(a) - Number(b)).forEach((idx) => {
        const fields = lazyState.get(idx) || {};
        Object.keys(fields).sort().forEach((name) => {
          const input = document.createElement('input');
          input.type = 'hidden';
          input.name = name;
          input.value = fields[name];
          frag.appendChild(input);
        });
      });
      if (predictionFeedbackRows.size) {
        const input = document.createElement('input');
        input.type = 'hidden';
        input.name = 'prediction_feedback_json';
        input.value = JSON.stringify({
          items: Array.from(predictionFeedbackRows.values()).sort((a, b) => Number(a.row_index) - Number(b.row_index)),
        });
        frag.appendChild(input);
      }
      lazyFields.appendChild(frag);
    }

    function selectedRowIndexesForCommit() {
      return new Set(
        Array.from(reviewForm.querySelectorAll('.row-check[name^="row_"]:checked'))
          .map((input) => String(input.name || '').replace(/^row_/, ''))
          .filter((idx) => idx !== '')
      );
    }

    function pruneUncheckedRowsForCommit() {
      const selectedRows = selectedRowIndexesForCommit();
      reviewForm.querySelectorAll('tr[data-row-index]').forEach((tr) => {
        const rowIndex = String(tr.dataset.rowIndex || '');
        if (selectedRows.has(rowIndex)) return;
        tr.querySelectorAll('input[name], select[name], textarea[name]').forEach((field) => {
          field.disabled = true;
        });
      });
      Array.from(lazyState.keys()).forEach((rowIndex) => {
        if (!selectedRows.has(String(rowIndex))) lazyState.delete(rowIndex);
      });
      Array.from(predictionFeedbackRows.keys()).forEach((rowIndex) => {
        if (!selectedRows.has(String(rowIndex))) predictionFeedbackRows.delete(rowIndex);
      });
      renderHiddenFields();
    }

    function persistModal(modalEl) {
      if (!modalEl || !activeLazy) return;
      if (activeLazy.type === 'split' && modalEl.id !== 'importSplitModal') return;
      if (activeLazy.type === 'transfer' && modalEl.id !== 'importTransferModal') return;
      const rowIndex = activeLazy.rowIndex;
      const prior = rowState(rowIndex);
      const next = {};
      const prefixMatchers = activeLazy.type === 'split'
        ? [/^(exp|inc)_amount_\d+_/, /^(exp|inc)_remainder_\d+$/]
        : [/^is_transfer_\d+$/, /^transfer_account_\d+$/, /^trf_amt_\d+_/, /^trf_remainder_\d+$/, /^trf_from_amt_\d+_/, /^trf_from_remainder_\d+$/];

      Object.entries(prior).forEach(([name, value]) => {
        if (!prefixMatchers.some((re) => re.test(name))) next[name] = value;
      });

      modalEl.querySelectorAll('input[name], select[name]').forEach((el) => {
        if (el.matches('select.env-mode')) return;
        const name = el.getAttribute('name');
        if (!name) return;
        if (el.type === 'checkbox') {
          if (el.checked) next[name] = el.value || '1';
          return;
        }
        const value = (el.value || '').trim();
        if (value && value !== '0' && value !== '0.00') next[name] = value;
      });

      if (activeLazy.type === 'transfer' && !next[`is_transfer_${rowIndex}`]) {
        Object.keys(next).forEach((name) => {
          if (/^(is_transfer|transfer_account|trf_amt|trf_remainder|trf_from_amt|trf_from_remainder)_/.test(name)) {
            delete next[name];
          }
        });
      }

      if (activeLazy.type === 'transfer' && !rowHasAnyTransferConfig(rowIndex, next)) {
        Object.keys(next).forEach((name) => {
          if (/^(is_transfer|transfer_account|trf_amt|trf_remainder|trf_from_amt|trf_from_remainder)_/.test(name)) {
            delete next[name];
          }
        });
      }

      if (activeLazy.type === 'split' && rowHasAnySplitConfig(rowIndex, next)) {
        clearTransferFields(next, rowIndex);
      }

      if (activeLazy.type === 'transfer' && rowHasAnyTransferConfig(rowIndex, next)) {
        clearSplitFields(next, rowIndex);
      }

      if (Object.keys(next).length) lazyState.set(String(rowIndex), next);
      else lazyState.delete(String(rowIndex));
      renderHiddenFields();
    }

    function setLazyField(rowIndex, name, value) {
      const idx = String(rowIndex);
      const fields = { ...(lazyState.get(idx) || {}) };
      if (value === null || value === undefined || value === '') delete fields[name];
      else fields[name] = String(value);
      if (Object.keys(fields).length) lazyState.set(idx, fields);
      else lazyState.delete(idx);
    }

    function clearLazyFieldsForRow(rowIndex, matchers) {
      const idx = String(rowIndex);
      const fields = { ...(lazyState.get(idx) || {}) };
      Object.keys(fields).forEach((name) => {
        if (matchers.some((re) => re.test(name))) delete fields[name];
      });
      if (Object.keys(fields).length) lazyState.set(idx, fields);
      else lazyState.delete(idx);
      renderHiddenFields();
    }

    function clearInputAndNotify(input) {
      input.value = '';
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
    }

    function clearSelectAndNotify(select) {
      select.value = '';
      select.dispatchEvent(new Event('change', { bubbles: true }));
    }

    function clearSplitModalDraft(rowIndex) {
      const idx = String(rowIndex);
      const modal = activeLazy?.type === 'split' && activeLazy.rowIndex === idx ? splitModal : null;
      modal?.querySelectorAll('input.env-input').forEach(clearInputAndNotify);
      modal?.querySelectorAll('[data-remainder-select]').forEach(clearSelectAndNotify);
      clearLazyFieldsForRow(idx, [
        new RegExp(`^(exp|inc)_amount_${idx}_`),
        new RegExp(`^(exp|inc)_remainder_${idx}$`),
      ]);
      updateSplitButtonStyle(idx, rowHasAnySplitConfig(idx));
      updatePredictionButtonStyle(idx);
    }

    function clearTransferModalDraft(rowIndex) {
      const idx = String(rowIndex);
      const modal = activeLazy?.type === 'transfer' && activeLazy.rowIndex === idx ? transferModal : null;
      modal?.querySelectorAll('input.env-input').forEach(clearInputAndNotify);
      modal?.querySelectorAll('[data-remainder-select], select[name="transfer_account_' + idx + '"]').forEach(clearSelectAndNotify);
      modal?.querySelectorAll('input[name="is_transfer_' + idx + '"]').forEach((checkbox) => {
        checkbox.checked = false;
        checkbox.dispatchEvent(new Event('change', { bubbles: true }));
      });
      clearLazyFieldsForRow(idx, [
        new RegExp(`^is_transfer_${idx}$`),
        new RegExp(`^transfer_account_${idx}$`),
        new RegExp(`^trf_amt_${idx}_`),
        new RegExp(`^trf_remainder_${idx}$`),
        new RegExp(`^trf_from_amt_${idx}_`),
        new RegExp(`^trf_from_remainder_${idx}$`),
      ]);
      updateTransferButtonStyle(idx, rowHasAnyTransferConfig(idx));
      updatePredictionButtonStyle(idx);
    }

    function clearModalBody(modalEl) {
      modalEl?.querySelectorAll('[data-role="split-body"], [data-role="transfer-body"]').forEach((body) => {
        body.innerHTML = '';
      });
      activeLazy = null;
    }

    function envelopeSelectorHtml({ scopeId, accountSelector, totalSelector, inputPrefix, remainderName, showMode, showDefaultButtons = false, values, targetSign = 1, legacyOutflow = false, blankAccountRemainderMode = 'all', validationLabel = '', validationToggle = '' }) {
      const rows = envelopes.map((e) => {
        const eid = String(e.id);
        const defCents = parseInt(e.default_amount_cents || 0, 10) || 0;
        const inputName = `${inputPrefix}_${eid}`;
        const amountValue = values[inputName] || '';
        const modeName = `${inputPrefix}_mode_${eid}`;
        return `
          <div class="env-row"
               data-env-row
               data-locked-account-id="${e.locked_account_id == null ? '' : esc(e.locked_account_id)}"
               data-envelope-id="${esc(eid)}"
               data-envelope-name="${esc(e.name || '')}"
               data-default-cents="${defCents}">
            <div class="env-main ${showMode ? 'env-showmode' : ''}">
              <div class="env-name">
                ${esc(e.name || '')}
                ${e.archived_at ? '<span class="badge text-bg-secondary ms-1">Archived</span>' : ''}
                <div class="env-meta" data-balance-wrap data-show-zero-balance="1" hidden>
                  Balance:
                  <span data-env-balance data-envelope-id="${esc(eid)}">$0.00</span>
                  ${showDefaultButtons && defCents !== 0 ? `<span class="ms-2">Default: <span>${money(defCents)}</span></span>` : ''}
                </div>
              </div>
              ${showMode ? `
                <div>
                  <select class="form-select form-select-sm env-mode"
                          name="${esc(modeName)}"
                          style="width: 6.5rem;"
                          aria-label="Income mode for ${esc(e.name || '')}">
                    <option value="add">Add</option>
                    <option value="set">Set</option>
                  </select>
                </div>` : ''}
              <div class="d-flex align-items-center gap-2">
                ${showDefaultButtons && defCents !== 0 ? `
                  <button type="button"
                          class="btn btn-outline-secondary btn-sm env-default-btn"
                          title="Set to default for ${esc(e.name || '')}"
                          data-env-default>Default</button>` : ''}
                <div class="input-group input-group-sm env-amt">
                  <span class="input-group-text">$</span>
                  <input type="number"
                         inputmode="decimal"
                         step="0.01"
                         class="form-control text-end env-input"
                         name="${esc(inputName)}"
                         value="${esc(amountValue)}"
                         placeholder="0.00"
                         aria-label="Amount for ${esc(e.name || '')}">
                </div>
              </div>
            </div>
          </div>`;
      }).join('');

      return `
        <div class="env-scope border rounded p-2"
             data-scope-id="${esc(scopeId)}"
             data-account-selector="${esc(accountSelector)}"
             data-total-selector="${esc(totalSelector)}"
             data-validation-label="${esc(validationLabel)}"
             data-validation-toggle="${esc(validationToggle)}"
             data-blank-account-remainder-mode="${esc(blankAccountRemainderMode)}"
             data-remainder-target-sign="${esc(targetSign)}"
             data-remainder-legacy-outflow="${legacyOutflow ? 1 : 0}">
          <div class="d-flex justify-content-between align-items-center mb-2">
            <div class="small text-muted">Enter signed envelope amounts; positives add, negatives subtract.</div>
            <div class="env-remaining">
              <small class="text-muted">Remaining</small>
              <strong class="ms-2" data-remaining-amount>$0.00</strong>
            </div>
          </div>
          <div class="vstack gap-2" data-env-list style="max-height: 260px; overflow:auto;">
            ${rows}
          </div>
          <div class="mt-2">
            <div class="d-flex flex-column flex-sm-row align-items-sm-end gap-2">
              <div class="flex-grow-1">
                <label class="form-label small mb-1">Remainder to</label>
                <select class="form-select form-select-sm" name="${esc(remainderName)}" data-remainder-select>
                  <option value="">-- none --</option>
                </select>
              </div>
              <div class="small text-sm-end" data-remainder-amount-wrap>
                <div class="text-muted">Remainder amount</div>
                <strong class="text-muted" data-remainder-amount>No remainder assigned</strong>
              </div>
            </div>
          </div>
        </div>`;
    }

    function applyRemainderValue(body, name, value) {
      if (!value) return;
      const rem = body.querySelector(`select[name="${CSS.escape(name)}"]`);
      if (!rem) return;
      let seed = body.querySelector(`input[data-remainder-initial][data-for="${CSS.escape(name)}"]`);
      if (!seed) {
        seed = document.createElement('input');
        seed.type = 'hidden';
        seed.dataset.remainderInitial = '';
        seed.dataset.for = name;
        seed.setAttribute('data-remainder-initial', '');
        rem.after(seed);
      }
      seed.value = value;
    }

    function initDynamicEnvelopeFilters() {
      if (typeof window.__applyEnvelopeFilters === 'function') {
        window.__applyEnvelopeFilters();
      }
    }

    function envelopeName(envelopeId) {
      const id = String(envelopeId || '');
      const envelope = envelopes.find((item) => String(item.id) === id);
      return envelope?.name || `Envelope ${id}`;
    }

    function accountName(accountId) {
      const id = String(accountId || '');
      const account = accounts.find((item) => String(item.id) === id);
      return account?.name || (id ? `Account ${id}` : 'Select account');
    }

    function centsFromAmountValue(value) {
      const raw = String(value ?? '').replace(/[$,\s]/g, '').trim();
      if (!raw) return 0;
      const number = Number(raw);
      return Number.isFinite(number) ? Math.round(number * 100) : 0;
    }

    function nonZeroCents(value) {
      return centsFromAmountValue(value) !== 0;
    }

    function fieldsWithOpenModalValues(rowIndex, type) {
      const idx = String(rowIndex);
      const fields = { ...(rowState(idx) || {}) };
      const modal = activeLazy?.type === type && activeLazy.rowIndex === idx
        ? (type === 'split' ? splitModal : transferModal)
        : null;
      if (!modal) return fields;

      modal.querySelectorAll('input[name], select[name]').forEach((el) => {
        if (el.matches('select.env-mode')) return;
        const name = el.getAttribute('name');
        if (!name) return;
        if (el.type === 'checkbox') {
          if (el.checked) fields[name] = el.value || '1';
          else delete fields[name];
          return;
        }
        const value = String(el.value || '').trim();
        if (value && value !== '0' && value !== '0.00') fields[name] = value;
        else delete fields[name];
      });
      return fields;
    }

    function amountPreviewRows(fields, prefix, rowIndex, targetSign = 1, remainderName = '') {
      const idx = String(rowIndex);
      const rows = [];
      let enteredCents = 0;
      const prefixWithRow = `${prefix}_${idx}_`;

      Object.entries(fields).forEach(([name, value]) => {
        if (!name.startsWith(prefixWithRow) || !nonZeroCents(value)) return;
        const envelopeId = name.slice(prefixWithRow.length);
        const cents = centsFromAmountValue(value);
        enteredCents += cents;
        rows.push({ label: envelopeName(envelopeId), cents });
      });

      const remainderEnvelopeId = remainderName ? fields[remainderName] : '';
      if (remainderEnvelopeId) {
        const tr = rowFor(idx);
        const totalCents = Math.abs(parseInt(tr?.dataset.amountCents || '0', 10) || 0);
        const targetCents = targetSign < 0 ? -totalCents : totalCents;
        const remainderCents = targetCents - enteredCents;
        if (remainderCents !== 0) {
          rows.push({ label: `Remainder to ${envelopeName(remainderEnvelopeId)}`, cents: remainderCents });
        }
      }

      return rows.sort((a, b) => a.label.localeCompare(b.label));
    }

    function previewRowsHtml(rows) {
      if (!rows.length) return '<div class="import-action-preview-empty">No amounts yet.</div>';
      return rows.map((row) => `
        <div class="import-action-preview-row">
          <span>${esc(row.label)}</span>
          <strong>${esc(money(row.cents))}</strong>
        </div>
      `).join('');
    }

    function splitPreviewHtml(rowIndex) {
      const idx = String(rowIndex);
      const tr = rowFor(idx);
      const isExpense = (tr?.dataset.section || '') === 'exp';
      const prefix = isExpense ? 'exp_amount' : 'inc_amount';
      const remainderName = `${isExpense ? 'exp' : 'inc'}_remainder_${idx}`;
      const rows = amountPreviewRows(
        fieldsWithOpenModalValues(idx, 'split'),
        prefix,
        idx,
        isExpense ? -1 : 1,
        remainderName
      );
      return `
        <div class="import-action-preview-title">${isExpense ? 'Split' : 'Distribution'}</div>
        ${previewRowsHtml(rows)}
      `;
    }

    function transferPreviewHtml(rowIndex) {
      const idx = String(rowIndex);
      const tr = rowFor(idx);
      const fields = fieldsWithOpenModalValues(idx, 'transfer');
      const isOut = (parseInt(tr?.dataset.amountCents || '0', 10) || 0) < 0;
      const currentAccount = accountName(accountSel?.value || '');
      const otherAccount = accountName(fields[`transfer_account_${idx}`] || '');
      const fromAccount = isOut ? currentAccount : otherAccount;
      const toAccount = isOut ? otherAccount : currentAccount;
      const fromRows = amountPreviewRows(
        fields,
        isOut ? 'trf_from_amt' : 'trf_amt',
        idx,
        isOut ? -1 : -1,
        isOut ? `trf_from_remainder_${idx}` : `trf_remainder_${idx}`
      );
      const toRows = amountPreviewRows(
        fields,
        isOut ? 'trf_amt' : 'trf_from_amt',
        idx,
        isOut ? 1 : 1,
        isOut ? `trf_remainder_${idx}` : `trf_from_remainder_${idx}`
      );
      return `
        <div class="import-action-preview-title">Transfer</div>
        <div class="import-action-preview-account"><span>From</span><strong>${esc(fromAccount)}</strong></div>
        <div class="import-action-preview-account"><span>To</span><strong>${esc(toAccount)}</strong></div>
        <div class="import-action-preview-section">From envelopes</div>
        ${previewRowsHtml(fromRows)}
        <div class="import-action-preview-section">To envelopes</div>
        ${previewRowsHtml(toRows)}
      `;
    }

    const actionPreview = document.createElement('div');
    actionPreview.id = 'importActionPreviewPopover';
    actionPreview.className = 'import-action-preview-popover';
    actionPreview.setAttribute('role', 'tooltip');
    actionPreview.setAttribute('aria-hidden', 'true');
    document.body.appendChild(actionPreview);

    const sourceTooltip = document.createElement('div');
    sourceTooltip.id = 'importSourceTooltipPopover';
    sourceTooltip.className = 'import-source-tooltip-popover';
    sourceTooltip.setAttribute('role', 'tooltip');
    sourceTooltip.setAttribute('aria-hidden', 'true');
    document.body.appendChild(sourceTooltip);

    function positionSourceTooltip(anchor) {
      const rect = anchor.getBoundingClientRect();
      const margin = 8;
      sourceTooltip.style.left = `${margin}px`;
      sourceTooltip.style.top = `${margin}px`;
      sourceTooltip.classList.add('is-visible');
      const popRect = sourceTooltip.getBoundingClientRect();
      const left = Math.min(
        Math.max(rect.left, margin),
        Math.max(margin, window.innerWidth - popRect.width - margin)
      );
      const top = Math.min(
        Math.max(rect.top, margin),
        Math.max(margin, window.innerHeight - popRect.height - margin)
      );
      sourceTooltip.style.left = `${left}px`;
      sourceTooltip.style.top = `${top}px`;
    }

    function showSourceTooltip(anchor) {
      if (!anchor) return;
      const text = String(anchor.dataset.payeeTooltip || '').trim();
      if (!text) {
        hideSourceTooltip();
        return;
      }
      sourceTooltip.textContent = text;
      sourceTooltip.setAttribute('aria-hidden', 'false');
      positionSourceTooltip(anchor);
    }

    function hideSourceTooltip() {
      sourceTooltip.classList.remove('is-visible');
      sourceTooltip.setAttribute('aria-hidden', 'true');
    }

    function positionActionPreview(anchor) {
      const rect = anchor.getBoundingClientRect();
      const margin = 8;
      actionPreview.style.left = `${margin}px`;
      actionPreview.style.top = `${margin}px`;
      actionPreview.classList.add('is-visible');
      const popRect = actionPreview.getBoundingClientRect();
      const left = Math.min(
        Math.max(rect.left, margin),
        Math.max(margin, window.innerWidth - popRect.width - margin)
      );
      const below = rect.bottom + margin;
      const above = rect.top - popRect.height - margin;
      const top = below + popRect.height <= window.innerHeight - margin
        ? below
        : Math.max(margin, above);
      actionPreview.style.left = `${left}px`;
      actionPreview.style.top = `${top}px`;
    }

    function showActionPreview(anchor) {
      if (!anchor || anchor.disabled) return;
      const rowIndex = anchor.dataset.rowIndex;
      if (!rowIndex) return;
      const isTransfer = anchor.classList.contains('trf-btn');
      const isActive = isTransfer
        ? rowHasAnyTransferConfig(rowIndex)
        : rowHasAnySplitConfig(rowIndex);
      if (!isActive) {
        hideActionPreview();
        return;
      }
      actionPreview.innerHTML = anchor.classList.contains('trf-btn')
        ? transferPreviewHtml(rowIndex)
        : splitPreviewHtml(rowIndex);
      actionPreview.setAttribute('aria-hidden', 'false');
      positionActionPreview(anchor);
    }

    function hideActionPreview() {
      actionPreview.classList.remove('is-visible');
      actionPreview.setAttribute('aria-hidden', 'true');
    }

    function actionPreviewFocusEnabled() {
      const configured = window.FITFT_ACCESSIBILITY?.importActionPreviewFocus;
      if (typeof configured === 'boolean') return configured;
      try {
        return window.localStorage?.getItem('fitft.importActionPreviewFocus') === '1';
      } catch {
        return false;
      }
    }

    function renderSplitModal(trigger) {
      const rowIndex = trigger?.dataset.rowIndex;
      const tr = rowFor(rowIndex);
      if (!rowIndex || !tr) return;
      const section = trigger.dataset.section || tr.dataset.section;
      const isExpense = section === 'exp';
      const state = rowState(rowIndex);
      const cents = Math.abs(parseInt(tr.dataset.amountCents || '0', 10) || 0);
      const modalEl = document.getElementById('importSplitModal');
      const title = modalEl?.querySelector('[data-role="split-title"]');
      const body = modalEl?.querySelector('[data-role="split-body"]');
      if (!modalEl || !body) return;

      activeLazy = { type: 'split', rowIndex: String(rowIndex) };
      if (title) title.textContent = `${isExpense ? 'Split Expense' : 'Distribute Income'} — ${rowLabel(tr)}`;
      body.innerHTML = `
        <div class="small text-muted mb-2">
          ${isExpense
            ? 'Enter envelope amounts for this expense (positive amounts still work; negatives subtract explicitly).'
            : 'Add/Subtract per envelope and optionally choose a "Remainder to ..." envelope.'}
        </div>
        <input type="hidden" id="${isExpense ? 'exp' : 'inc'}_total_${esc(rowIndex)}" value="${esc(dollarsFromCents(cents))}">
        ${envelopeSelectorHtml({
          scopeId: `${isExpense ? 'exp' : 'inc'}-${rowIndex}`,
          accountSelector: '#importAccount',
          totalSelector: `#${isExpense ? 'exp' : 'inc'}_total_${rowIndex}`,
          inputPrefix: `${isExpense ? 'exp' : 'inc'}_amount_${rowIndex}`,
          remainderName: `${isExpense ? 'exp' : 'inc'}_remainder_${rowIndex}`,
          showMode: !isExpense,
          showDefaultButtons: true,
          values: state,
          targetSign: isExpense ? -1 : 1,
          legacyOutflow: isExpense,
          validationLabel: isExpense ? 'expense split' : 'income split',
        })}
      `;
      applyRemainderValue(body, `${isExpense ? 'exp' : 'inc'}_remainder_${rowIndex}`, state[`${isExpense ? 'exp' : 'inc'}_remainder_${rowIndex}`]);
      initDynamicEnvelopeFilters();
    }

    function accountOptionsHtml(rowIndex, selectedValue) {
      const currentAccount = String(accountSel?.value || '');
      let html = '<option value="">Select account...</option>';
      accounts.forEach((a) => {
        const id = String(a.id);
        if (id === currentAccount && id !== String(selectedValue || '')) return;
        html += `<option value="${esc(id)}" ${id === String(selectedValue || '') ? 'selected' : ''}>${esc(a.name || '')}</option>`;
      });
      return html;
    }

    function renderTransferModal(trigger) {
      const rowIndex = trigger?.dataset.rowIndex;
      const tr = rowFor(rowIndex);
      if (!rowIndex || !tr) return;
      const state = rowState(rowIndex);
      const cents = Math.abs(parseInt(tr.dataset.amountCents || '0', 10) || 0);
      const isOut = (parseInt(tr.dataset.amountCents || '0', 10) || 0) < 0;
      const modalEl = document.getElementById('importTransferModal');
      const title = modalEl?.querySelector('[data-role="transfer-title"]');
      const body = modalEl?.querySelector('[data-role="transfer-body"]');
      if (!modalEl || !body) return;

      activeLazy = { type: 'transfer', rowIndex: String(rowIndex) };
      const checked = state[`is_transfer_${rowIndex}`] !== '0';
      if (title) title.textContent = `Account Transfer — ${rowLabel(tr)}`;
      body.innerHTML = `
        <div class="form-check mb-2">
          <input class="form-check-input" type="checkbox" id="isTransfer_${esc(rowIndex)}" name="is_transfer_${esc(rowIndex)}" value="1" ${checked ? 'checked' : ''}>
          <label class="form-check-label" for="isTransfer_${esc(rowIndex)}">
            Mark this imported row as an inter-account transfer
          </label>
        </div>

        <div class="row g-3 align-items-end mb-3">
          <div class="col-md-7">
            <label class="form-label">Other account</label>
            <select class="form-select" id="trfAccount-${esc(rowIndex)}" name="transfer_account_${esc(rowIndex)}">
              ${accountOptionsHtml(rowIndex, state[`transfer_account_${rowIndex}`])}
            </select>
          </div>
          <div class="col-md-5">
            <div class="small text-muted">
              Assign signed envelope amounts in the other account below. The net must equal that transfer leg.
            </div>
          </div>
        </div>

        <input type="hidden" id="trf_total_${esc(rowIndex)}" value="${esc(dollarsFromCents(cents))}">
        ${envelopeSelectorHtml({
          scopeId: `trf-${rowIndex}`,
          accountSelector: `#trfAccount-${rowIndex}`,
          totalSelector: `#trf_total_${rowIndex}`,
          inputPrefix: `trf_amt_${rowIndex}`,
          remainderName: `trf_remainder_${rowIndex}`,
          showMode: false,
          showDefaultButtons: true,
          values: state,
          targetSign: isOut ? 1 : -1,
          legacyOutflow: !isOut,
          blankAccountRemainderMode: 'global',
          validationLabel: 'other-account transfer envelopes',
          validationToggle: `#isTransfer_${rowIndex}`,
        })}

        <hr class="my-3">

        <div class="mb-2 fw-semibold">From this account — envelope amounts to ${isOut ? 'MOVE OUT OF' : 'MOVE INTO'}</div>
        <input type="hidden" id="from_total_${esc(rowIndex)}" value="${esc(dollarsFromCents(cents))}">
        ${envelopeSelectorHtml({
          scopeId: `trf-from-${rowIndex}`,
          accountSelector: '#importAccount',
          totalSelector: `#from_total_${rowIndex}`,
          inputPrefix: `trf_from_amt_${rowIndex}`,
          remainderName: `trf_from_remainder_${rowIndex}`,
          showMode: false,
          showDefaultButtons: true,
          values: state,
          targetSign: isOut ? -1 : 1,
          legacyOutflow: isOut,
          blankAccountRemainderMode: 'global',
          validationLabel: 'current-account transfer envelopes',
          validationToggle: `#isTransfer_${rowIndex}`,
        })}
      `;
      applyRemainderValue(body, `trf_remainder_${rowIndex}`, state[`trf_remainder_${rowIndex}`]);
      applyRemainderValue(body, `trf_from_remainder_${rowIndex}`, state[`trf_from_remainder_${rowIndex}`]);
      initDynamicEnvelopeFilters();
    }

    // -------- FIN-063: server-backed import-review draft recovery --------
    let draftSaveTimer = null;
    let draftRestoring = false;
    let draftDirty = false;

    function selectedAccountId() {
      return String(accountSel?.value || '');
    }

    function draftScopeMatchesCurrentAccount() {
      return !!draftScopeAccountId && selectedAccountId() === draftScopeAccountId;
    }

    function refreshDraftFingerprintInput() {
      if (!draftFingerprintInput) return;
      draftFingerprintInput.value = draftScopeMatchesCurrentAccount() ? String(importDraftIdentity.fingerprint || '') : '';
    }

    function rowDraftFingerprint(rowIndex) {
      const idx = String(rowIndex);
      const state = storedRow(idx);
      if (state.draftRowFingerprint) return state.draftRowFingerprint;
      const item = (importDraftIdentity.row_fingerprints || []).find((row) => String(row.row_index) === idx);
      return String(item?.fingerprint || '');
    }

    function collectDraftRows() {
      persistModal(splitModal);
      persistModal(transferModal);
      const rows = {};
      document.querySelectorAll('tr[data-row-index]').forEach((tr) => {
        const idx = String(tr.getAttribute('data-row-index') || '');
        if (!idx) return;
        const check = tr.querySelector(`input[name="row_${CSS.escape(idx)}"]`);
        const quick = tr.querySelector(`select[name="exp_single_${CSS.escape(idx)}"]`);
        const payee = tr.querySelector(`input[name="payee_${CSS.escape(idx)}"]`);
        const memo = tr.querySelector(`input[name="memo_${CSS.escape(idx)}"]`);
        const matchTx = tr.querySelector(`input[name="match_tx_${CSS.escape(idx)}"]`);
        const matchSrc = tr.querySelector(`input[name="match_amt_src_${CSS.escape(idx)}"]`);
        rows[idx] = {
          row_fingerprint: rowDraftFingerprint(idx),
          checked: !!check?.checked,
          payee: payee?.value || '',
          memo: memo?.value || '',
          quick_envelope_id: quick?.value || '',
          lazy_fields: { ...(lazyState.get(idx) || {}) },
          match_tx_id: matchTx?.value || '',
          match_amount_source: matchSrc?.value || '',
        };
      });
      return rows;
    }

    function collectIgnoredManualCandidateIds() {
      return Array.from(document.querySelectorAll('input[name="ignore_tx[]"]:checked'))
        .map((el) => String(el.value || '').trim())
        .filter(Boolean);
    }

    function currentDraftPayload() {
      return {
        version: 1,
        saved_at: new Date().toISOString(),
        rows: collectDraftRows(),
        ignored_manual_candidate_ids: collectIgnoredManualCandidateIds(),
      };
    }

    async function saveDraftNow() {
      if (draftRestoring || !draftSaveUrl || !importDraftIdentity.fingerprint || !draftScopeMatchesCurrentAccount()) return;
      draftDirty = false;
      try {
        const response = await fetch(draftSaveUrl, {
          method: 'POST',
          headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' },
          body: JSON.stringify({
            fingerprint: importDraftIdentity.fingerprint,
            account_id: draftScopeAccountId,
            source_type: importDraftIdentity.source_type || 'unknown',
            source_filename: importDraftIdentity.source_filename || '',
            file_sha256: importDraftIdentity.file_sha256 || '',
            row_count: importDraftIdentity.row_count || 0,
            expires_at: importDraftIdentity.expires_at || '',
            draft: currentDraftPayload(),
          }),
        });
        if (!response.ok) draftDirty = true;
      } catch (e) {
        draftDirty = true;
        console.warn('import draft save failed', e);
      }
    }

    function scheduleDraftSave() {
      if (draftRestoring) return;
      draftDirty = true;
      if (draftSaveTimer) window.clearTimeout(draftSaveTimer);
      draftSaveTimer = window.setTimeout(saveDraftNow, 700);
    }

    function flushDraftSave() {
      if (draftSaveTimer) window.clearTimeout(draftSaveTimer);
      draftSaveTimer = null;
      if (!draftDirty) return;
      if (navigator.sendBeacon && draftSaveUrl && importDraftIdentity.fingerprint && draftScopeMatchesCurrentAccount()) {
        const body = JSON.stringify({
          fingerprint: importDraftIdentity.fingerprint,
          account_id: draftScopeAccountId,
          source_type: importDraftIdentity.source_type || 'unknown',
          source_filename: importDraftIdentity.source_filename || '',
          file_sha256: importDraftIdentity.file_sha256 || '',
          row_count: importDraftIdentity.row_count || 0,
          expires_at: importDraftIdentity.expires_at || '',
          draft: currentDraftPayload(),
        });
        if (navigator.sendBeacon(draftSaveUrl, new Blob([body], { type: 'application/json' }))) {
          draftDirty = false;
          return;
        }
      }
      saveDraftNow();
    }

    function setInputValueAndNotify(input, value) {
      if (!input || input.disabled) return false;
      input.value = value == null ? '' : String(value);
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
      return true;
    }

    function originalPayeeTooltipText(payee, memo) {
      const payeeText = String(payee || '').trim();
      const memoText = String(memo || '').trim();
      if (!memoText) return payeeText;
      if (!payeeText) return memoText;
      const payeeLower = payeeText.toLowerCase();
      const memoLower = memoText.toLowerCase();
      if (memoLower === payeeLower) return payeeText;
      if (memoLower.startsWith(payeeLower)) return memoText;
      if (payeeLower.startsWith(memoLower)) return payeeText;
      return `${payeeText} - ${memoText}`;
    }

    function syncPayeeTooltip(input) {
      if (!input) return;
      const tr = input.closest('tr');
      const rowIndex = tr?.dataset.rowIndex || input.name?.replace(/^payee_/, '') || '';
      const tooltip = input.closest('.import-payee-hover') || tr?.querySelector('.import-payee-hover');
      const originalPayee = tr?.querySelector(`input[name="orig_payee_${CSS.escape(rowIndex)}"]`)?.value || '';
      const originalMemo = tr?.querySelector(`input[name="orig_memo_${CSS.escape(rowIndex)}"]`)?.value || '';
      if (tooltip) tooltip.dataset.payeeTooltip = originalPayeeTooltipText(originalPayee, originalMemo) || input.value || '';
    }

    function syncAllPayeeTooltips() {
      reviewForm.querySelectorAll('.import-payee-input').forEach(syncPayeeTooltip);
    }

    function setCheckValueAndNotify(input, checked) {
      if (!input || input.disabled) return false;
      input.checked = !!checked;
      input.dispatchEvent(new Event('change', { bubbles: true }));
      return true;
    }

    function autoCheckImportRow(rowIndex) {
      const tr = rowFor(rowIndex);
      const checkbox = tr?.querySelector(`input[name="row_${CSS.escape(String(rowIndex))}"]`);
      return setCheckValueAndNotify(checkbox, true);
    }

    function restoreQuickEnvelope(rowIndex, envelopeId) {
      const quick = rowFor(rowIndex)?.querySelector(`select[name="exp_single_${CSS.escape(String(rowIndex))}"]`);
      if (!quick || quick.disabled || !envelopeId) return false;
      const option = Array.from(quick.options).find((opt) => opt.value === String(envelopeId));
      if (!option || option.hidden || option.disabled) return false;
      quick.value = String(envelopeId);
      updateQuickAssignPlaceholderState(quick);
      return true;
    }

    function lazyFieldIsStillValid(name, value, rowIndex) {
      const val = String(value || '').trim();
      if (!val) return false;
      if (name === `transfer_account_${rowIndex}`) return accountExistsForTransfer(val);
      const envelopeField = /_(\d+)$/.exec(name);
      if (/(remainder|amount)_/.test(name) || /_remainder_/.test(name)) {
        if (name.includes('_amount_') || name.includes('_amt_')) return envelopeField ? envelopeExists(envelopeField[1]) : true;
        return envelopeExists(val);
      }
      return true;
    }

    function restoreLazyFields(rowIndex, fields) {
      const valid = {};
      Object.entries(fields || {}).forEach(([name, value]) => {
        if (lazyFieldIsStillValid(name, value, rowIndex)) valid[name] = String(value);
      });
      if (rowHasAnyTransferConfig(rowIndex, valid)) {
        clearSplitFields(valid, rowIndex);
      } else if (rowHasAnySplitConfig(rowIndex, valid)) {
        clearTransferFields(valid, rowIndex);
      }
      if (Object.keys(valid).length) lazyState.set(String(rowIndex), valid);
      else lazyState.delete(String(rowIndex));
      updateSplitButtonStyle(rowIndex, rowHasAnySplitConfig(rowIndex, valid));
      updateTransferButtonStyle(rowIndex, rowHasAnyTransferConfig(rowIndex, valid));
    }

    function restoreManualMatch(rowIndex, rowDraft) {
      const txId = String(rowDraft?.match_tx_id || '').trim();
      if (!txId || rowIsResolved(rowIndex)) return false;
      const sel = document.querySelector(`.man-match-exp[data-manual-id="${CSS.escape(txId)}"], .man-match-inc[data-manual-id="${CSS.escape(txId)}"]`);
      if (!sel || !Array.from(sel.options).some((opt) => opt.value === String(rowIndex))) return false;
      sel.value = String(rowIndex);
      const { tx, src } = ensureHiddenMatchInputs(rowIndex);
      if (tx) tx.value = txId;
      if (src) src.value = String(rowDraft.match_amount_source || 'import');
      setMatchedState(rowIndex, true);
      return true;
    }

    function restoreIgnoredManualCandidates(ids) {
      const wanted = new Set((ids || []).map(String));
      document.querySelectorAll('input[name="ignore_tx[]"]').forEach((cb) => {
        if (wanted.has(String(cb.value || ''))) setCheckValueAndNotify(cb, true);
      });
    }

    function applyImportReviewDraft(draftWrapper) {
      const draft = draftWrapper?.draft || draftWrapper || {};
      const rows = draft.rows || {};
      draftRestoring = true;
      try {
        Object.entries(rows).forEach(([rowIndex, rowDraft]) => {
          const idx = String(rowIndex);
          const tr = rowFor(idx);
          if (!tr || rowDraft?.row_fingerprint !== rowDraftFingerprint(idx)) return;
          if (rowIsResolved(idx)) return;
          setCheckValueAndNotify(tr.querySelector(`input[name="row_${CSS.escape(idx)}"]`), !!rowDraft.checked);
          setInputValueAndNotify(tr.querySelector(`input[name="payee_${CSS.escape(idx)}"]`), rowDraft.payee || '');
          setInputValueAndNotify(tr.querySelector(`input[name="memo_${CSS.escape(idx)}"]`), rowDraft.memo || '');
          restoreQuickEnvelope(idx, rowDraft.quick_envelope_id);
          restoreLazyFields(idx, rowDraft.lazy_fields || {});
        });
        renderHiddenFields();
        Object.entries(rows).forEach(([rowIndex, rowDraft]) => restoreManualMatch(String(rowIndex), rowDraft));
        restoreIgnoredManualCandidates(draft.ignored_manual_candidate_ids || []);
      } finally {
        draftRestoring = false;
      }
    }

    async function discardDraft() {
      if (!draftDiscardUrl || !importDraftIdentity.fingerprint || !draftScopeAccountId) return;
      try {
        await fetch(draftDiscardUrl, {
          method: 'POST',
          headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' },
          body: JSON.stringify({ fingerprint: importDraftIdentity.fingerprint, account_id: draftScopeAccountId }),
        });
      } catch (e) {
        console.warn('import draft discard failed', e);
      }
    }

    function showDraftRestoreModalIfNeeded() {
      if (!importReviewDraft || !importReviewDraft.draft || !importReviewDraft.fingerprint) return;
      if (!draftScopeMatchesCurrentAccount()) return;
      if (String(importReviewDraft.fingerprint) !== String(importDraftIdentity.fingerprint || '')) return;
      const modal = document.getElementById('importDraftRestoreModal');
      if (!modal) return;
      const summary = modal.querySelector('[data-role="draft-restore-summary"]');
      if (summary) {
        const saved = importReviewDraft.updated_at ? new Date(importReviewDraft.updated_at).toLocaleString() : 'recently';
        const file = importReviewDraft.source_filename || 'this file';
        const rows = importReviewDraft.row_count ? `${importReviewDraft.row_count} rows` : 'this import';
        summary.textContent = `${file} · ${rows} · saved ${saved}`;
      }
      const bsDraftModal = window.bootstrap ? new bootstrap.Modal(modal) : null;
      modal.querySelector('[data-role="draft-restore"]')?.addEventListener('click', () => {
        applyImportReviewDraft(importReviewDraft);
        bsDraftModal?.hide();
        scheduleDraftSave();
      }, { once: true });
      modal.querySelector('[data-role="draft-discard"]')?.addEventListener('click', async () => {
        await discardDraft();
        bsDraftModal?.hide();
      }, { once: true });
      if (bsDraftModal) bsDraftModal.show();
      else if (window.confirm('Restore saved import review draft?')) applyImportReviewDraft(importReviewDraft);
      else discardDraft();
    }

    // Section master toggles (skip dupes)
    function toggleSection(sectionClass, masterChecked) {
      document.querySelectorAll('.' + sectionClass).forEach(cb => {
        if (cb.disabled) return;
        cb.checked = masterChecked;
      });
    }
    document.getElementById('exp_select_all')?.addEventListener('change', (e) => {
      toggleSection('exp-row', e.target.checked);
    });
    document.getElementById('inc_select_all')?.addEventListener('change', (e) => {
      toggleSection('inc-row', e.target.checked);
    });

    document.addEventListener('click', (e) => {
      const sortHeader = e.target.closest('.import-sort-header');
      if (!sortHeader) return;
      sortImportSection(sortHeader.dataset.importSortSection || '', sortHeader.dataset.importSort || '');
    });

    // Filter "Quick assign" envelope options by selected account
    function updateQuickAssignPlaceholderState(sel) {
      const isMatched = !!sel.closest('tr')?.classList.contains('table-info');
      sel.classList.toggle('exp-quick-placeholder', !sel.value && !isMatched);
    }

    function filterQuickAssign() {
      const acc = document.getElementById('importAccount')?.value || '';
      document.querySelectorAll('select[data-role="exp-quick"]').forEach(sel => {
        Array.from(sel.options).forEach((opt, idx) => {
          if (idx === 0) return; // placeholder
          const lock = opt.getAttribute('data-locked-account-id') || '';
          const allowed = (lock === '' || lock === acc);
          opt.hidden = !allowed;
        });
        const selectedOpt = sel.selectedOptions[0];
        if (selectedOpt && selectedOpt.hidden) sel.value = '';
        updateQuickAssignPlaceholderState(sel);
      });
    }
    document.getElementById('importAccount')?.addEventListener('change', filterQuickAssign);
    document.querySelectorAll('select[data-role="exp-quick"]').forEach(sel => {
      sel.addEventListener('change', () => updateQuickAssignPlaceholderState(sel));
      updateQuickAssignPlaceholderState(sel);
    });
    filterQuickAssign();

    // -------- Collect imported rows (to populate manual selects / API payloads) --------
    function collectImports(options = {}) {
      const includeResolved = !!options.includeResolved;
      const out = { exp: [], inc: [] };
      document.querySelectorAll('table tbody tr').forEach(tr => {
        const amtInput = tr.querySelector('input[name^="amount_"]');
        if (!amtInput) return;
        const name = amtInput.getAttribute('name');
        const i = parseInt(name.split('_')[1], 10);
        if (isNaN(i)) return;
        const amtFloat = parseFloat(amtInput.value || '0') || 0;
        const cents = Math.round(amtFloat * 100);
        if (!includeResolved && rowIsResolved(i)) return;

        const tds = tr.querySelectorAll('td');
        const postedAt = tr.getAttribute('data-posted-at') || tds[1]?.textContent.trim() || '';
        const amountText = tds[2]?.textContent.trim() || '';
        const payeeInput = tr.querySelector(`input[name="payee_${i}"]`);
        const memoInput = tr.querySelector(`input[name="memo_${i}"]`);
        const payee = payeeInput?.value.trim() || tr.getAttribute('data-payee') || '';
        const memo  = memoInput?.value.trim() || '';
        const fitid = (tr.getAttribute('data-fitid') || '').trim();
        const label = `${postedAt} — ${amountText} — ${payee || 'No payee'}${memo ? (' — ' + memo) : ''}`;
        const rec = { i, cents, posted_at: postedAt, payee, memo, fitid, label, tr };
        const state = storedRow(i);
        state.section = cents < 0 ? 'exp' : cents > 0 ? 'inc' : 'zero';
        state.amountCents = cents;
        state.fitid = fitid;

        if (cents < 0) {
          tr.dataset.section = 'exp';
          tr.dataset.rowIndex = String(i);
          tr.dataset.amountCents = String(cents);
          out.exp.push(rec);
        } else if (cents > 0) {
          tr.dataset.section = 'inc';
          tr.dataset.rowIndex = String(i);
          tr.dataset.amountCents = String(cents);
          out.inc.push(rec);
        }
      });
      return out;
    }
    const imports = collectImports();

    function centsToParts(c){ c = parseInt(c,10)||0; const d = Math.trunc(Math.abs(c)/100); const r = Math.abs(c)%100; return {neg:c<0, d, r}; }
    function buildManualRowsTable(rows, isExpense) {
      const colorClass = isExpense ? "text-danger" : "text-success";
      const selClass = isExpense ? "man-match-exp" : "man-match-inc";
      return `
        <div class="table-responsive">
          <table class="table table-sm align-middle mb-0">
            <thead class="app-table-head">
              <tr>
                <th style="width:11rem;">Manual Date</th>
                <th style="width:9rem;">Amount</th>
                <th>${isExpense ? 'Payee / Memo' : 'Source / Memo'}</th>
                <th style="width:20rem;">Match to Imported</th>
              </tr>
            </thead>
            <tbody>
              ${rows.map(m => {
                const p = centsToParts(m.amount_cents);
                const amt = `${p.d}.${p.r<10?'0':''}${p.r}`;
                const payee = esc(m.payee || '-');
                const memo = (m.memo ? ` — ${esc(m.memo)}` : '');
                return `
                <tr>
                  <td>${esc(m.posted_at || '')}</td>
                  <td class="${colorClass} text-end">${amt}</td>
                  <td><div class="small"><strong>${payee}</strong><span class="text-muted">${memo}</span></div></td>
                  <td>
                    <div class="d-flex gap-2">
                      <select class="form-select form-select-sm ${selClass}"
                              data-manual-id="${esc(m.id)}"
                              data-manual-amount="${esc(m.amount_cents)}">
                        <option value="">Select imported ${isExpense ? 'expense' : 'income'}...</option>
                      </select>
                      <button type="button" class="btn btn-sm btn-outline-secondary man-clear" data-manual-id="${esc(m.id)}">Clear</button>
                      <label class="form-check-label small d-flex align-items-center gap-1">
                        <input class="form-check-input" type="checkbox" name="ignore_tx[]" value="${esc(m.id)}" data-range-checkbox>
                        Ignore
                      </label>
                    </div>
                  </td>
                </tr>`;
              }).join('')}
            </tbody>
          </table>
        </div>
      `;
    }

    function buildManualTable(title, rows, isExpense){
      if (!rows.length) return "";
      return `
      <div class="card mb-3" data-checkbox-range-scope>
        <div class="card-header fw-semibold">${title}</div>
        <div class="card-body p-2">
          ${buildManualRowsTable(rows, isExpense)}
        </div>
      </div>`;
    }

    function buildManualAccordion(title, rows, overflowRows, isExpense, key) {
      if (!rows.length && !overflowRows.length) return "";
      const collapseId = `manual-${key}-overflow`;
      const overflowLabel = `Show out-of-window manual ${isExpense ? 'expenses' : 'income'} (${overflowRows.length})`;
      return `
      <div class="card mb-3" data-checkbox-range-scope>
        <div class="card-header fw-semibold">${title}</div>
        <div class="card-body p-2">
          ${rows.length ? buildManualRowsTable(rows, isExpense) : '<div class="small text-muted">No manual transactions in the import date window.</div>'}
          ${overflowRows.length ? `
            <div class="accordion mt-2" id="manual-${key}-accordion">
              <div class="accordion-item">
                <h2 class="accordion-header">
                  <button class="accordion-button collapsed py-2" type="button" data-bs-toggle="collapse" data-bs-target="#${collapseId}" aria-expanded="false" aria-controls="${collapseId}">
                    ${overflowLabel}
                  </button>
                </h2>
                <div id="${collapseId}" class="accordion-collapse collapse" data-bs-parent="#manual-${key}-accordion">
                  <div class="accordion-body p-2">
                    ${buildManualRowsTable(overflowRows, isExpense)}
                  </div>
                </div>
              </div>
            </div>` : ''}
        </div>
      </div>`;
    }

    function importRowsPayload(rows) {
      return rows.map(r => ({
        index: r.i,
        posted_at: r.posted_at,
        amount_cents: r.cents,
        payee: r.payee,
        memo: r.memo,
        fitid: r.fitid
      }));
    }

    function statementSourcePayload() {
      return {
        import_source_token: document.querySelector('input[name="import_source_token"]')?.value || ''
      };
    }

    function duplicateRefreshPayload() {
      const current = collectImports({ includeResolved: true });
      return importRowsPayload(current.exp.concat(current.inc));
    }

    function manualCandidatesPayload() {
      const current = collectImports({ includeResolved: true });
      return importRowsPayload(current.exp.concat(current.inc));
    }

    function renderManualPanels(items, overflowItems = []){
      const exp = items.filter(x => (parseInt(x.amount_cents,10)||0) < 0);
      const inc = items.filter(x => (parseInt(x.amount_cents,10)||0) > 0);
      const overflowExp = overflowItems.filter(x => (parseInt(x.amount_cents,10)||0) < 0);
      const overflowInc = overflowItems.filter(x => (parseInt(x.amount_cents,10)||0) > 0);
      const expPanel = document.getElementById('manual-exp-panel');
      const incPanel = document.getElementById('manual-inc-panel');
      if (expPanel) expPanel.innerHTML = buildManualAccordion("Manual Expenses to Match", exp, overflowExp, true, 'exp');
      if (incPanel) incPanel.innerHTML = buildManualAccordion("Manual Income to Match", inc, overflowInc, false, 'inc');

      const refreshed = collectImports();
      populateManualSelects('.man-match-exp', refreshed.exp);
      populateManualSelects('.man-match-inc', refreshed.inc);
      applyAutoMatchSuggestions(items.concat(overflowItems, manualRuleSuggestionItems()));
    }

    async function fetchAndRenderManual(){
      const aid = accountSel?.value;
      if (!aid) {
        renderManualPanels([]);
        return;
      }
      try {
        const importsPayload = manualCandidatesPayload();
        const resp = await fetch(manualCandidatesUrl, {
          method: 'POST',
          headers: {
            'Accept': 'application/json',
            'Content-Type': 'application/json'
          },
          body: JSON.stringify({
            account_id: aid,
            imports: importsPayload,
            ...statementSourcePayload()
          })
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        renderManualPanels(data.items || [], data.overflow_items || []);
      } catch(e){
        console.warn("manual-candidates fetch failed:", e);
        renderManualPanels([]);
      }
    }

    let dupeRequest = null;
    function fetchDupesForAccount() {
      const aid = accountSel?.value;
      if (!aid || !dupesUrl) return Promise.resolve({ fitids: [], details: {}, row_indexes: [] });
      if (dupeRequest && dupeRequest.accountId === String(aid)) return dupeRequest.promise;
      const importsPayload = duplicateRefreshPayload();
      const promise = fetch(dupesUrl, {
          method: 'POST',
          headers: {
            'Accept': 'application/json',
            'Content-Type': 'application/json'
          },
          body: JSON.stringify({ account_id: aid, imports: importsPayload, ...statementSourcePayload() })
        })
        .then((resp) => resp.ok ? resp.json() : { fitids: [], details: {}, row_indexes: [] })
        .catch((e) => {
          console.warn('dupes fetch failed', e);
          return { fitids: [], details: {}, row_indexes: [] };
        });
      dupeRequest = { accountId: String(aid), promise };
      return promise;
    }

    async function refreshDupes() {
      const data = await fetchDupesForAccount();
      const dupeSet = new Set((data.fitids || []).map(String));
      const details = data.details || {};
      const duplicateRows = new Set((data.row_indexes || []).map(Number));

      document.querySelectorAll('table tbody tr[data-fitid]').forEach(tr => {
        const fit = (tr.getAttribute('data-fitid') || '').trim();
        const rowIndex = Number(tr.getAttribute('data-row-index'));
        const cb = tr.querySelector('.row-check');
        const splitBtn = tr.querySelector('.exp-split-btn, .inc-split-btn');
        const trfBtn = tr.querySelector('.trf-btn');
        const quick = tr.querySelector('select[data-role="exp-quick"]');
        setRowDuplicateState(rowIndex, (!!fit && dupeSet.has(fit)) || duplicateRows.has(rowIndex));
        const state = storedRow(rowIndex);
        const isDupe = !!state.duplicate;
        const isResolved = rowIsResolved(rowIndex);

        tr.classList.toggle('table-success', isDupe);
        if (cb) { cb.checked = !isResolved; cb.disabled = isResolved; }
        if (splitBtn) splitBtn.disabled = isResolved;
        if (trfBtn) trfBtn.disabled = isResolved;
        if (quick) quick.disabled = isResolved;
        updatePredictionButtonStyle(rowIndex);

        const payeeInput = tr.querySelector('input[name^="payee_"]');
        const memoInput  = tr.querySelector('input[name^="memo_"]');
        if (payeeInput) payeeInput.disabled = isResolved;
        if (memoInput)  memoInput.disabled  = isResolved;

        const info = fit ? details[fit] : null;
        if (info) {
          if (payeeInput && info.payee != null) payeeInput.value = info.payee;
          if (memoInput && info.memo != null) memoInput.value = info.memo;
        }
      });
    }

    function populateManualSelects(selector, items){
      document.querySelectorAll(selector).forEach(sel => {
        while (sel.options.length > 1) sel.remove(1);
        items.forEach(r => {
          const opt = document.createElement('option');
          opt.value = String(r.i);
          opt.textContent = r.label;
          opt.dataset.importAmount = String(r.cents);
          opt.title = opt.textContent;
          sel.appendChild(opt);
        });
      });
    }
    populateManualSelects('.man-match-exp', imports.exp);
    populateManualSelects('.man-match-inc', imports.inc);


    function applyAutoMatchSuggestions(items) {
      (items || []).forEach(item => {
        const suggested = item.suggested_import_index;
        if (suggested === null || suggested === undefined || suggested === '') return;
        const manualId = String(item.id);
        const sel = document.querySelector(`.man-match-exp[data-manual-id="${CSS.escape(manualId)}"], .man-match-inc[data-manual-id="${CSS.escape(manualId)}"]`);
        if (!sel) return;
        const value = String(suggested);
        if (!Array.from(sel.options).some(opt => opt.value === value)) return;
        sel.value = value;
        beginMatchFromManual(sel, parseInt(sel.getAttribute('data-manual-amount') || '0', 10) < 0 ? 'exp' : 'inc');
      });
    }

    function manualRuleSuggestionItems() {
      return (importPrefills || []).filter((prefill) => (
        prefill
        && prefill.prefill
        && prefill.manual_match
        && prefill.manual_match.transaction_id
        && prefill.row_index !== undefined
      )).map((prefill) => ({
        id: prefill.manual_match.transaction_id,
        suggested_import_index: prefill.row_index,
      }));
    }

    // -------- Matching UI: highlight + disable controls --------
    function ensureHiddenMatchInputs(i) {
      const tr = rowFor(i);
      if (!tr) return { tx: null, src: null };
      const fieldContainer = tr.querySelector('td:last-child');
      let tx = tr.querySelector(`input[name="match_tx_${i}"]`);
      let src = tr.querySelector(`input[name="match_amt_src_${i}"]`);
      if (!tx) {
        tx = document.createElement('input');
        tx.type = 'hidden';
        tx.name = `match_tx_${i}`;
        fieldContainer?.appendChild(tx);
      }
      if (!src) {
        src = document.createElement('input');
        src.type = 'hidden';
        src.name = `match_amt_src_${i}`;
        fieldContainer?.appendChild(src);
      }
      return { tx, src };
    }

    function setMatchedState(i, matched) {
      const tr = rowFor(i);
      if (!tr) return;
      const cb = tr.querySelector('.row-check');
      const quick = tr.querySelector('select[data-role="exp-quick"]');
      const splitBtn = tr.querySelector('.exp-split-btn, .inc-split-btn');
      const trfBtn = tr.querySelector('.trf-btn');

      if (matched) {
        clearCreationStateForRow(i);
        storedRow(i).matched = true;
        tr.classList.add('table-info');
        cb && (cb.checked = true);
        if (quick) {
          quick.disabled = true;
          quick.classList.remove('exp-quick-placeholder');
        }
        splitBtn && (splitBtn.disabled = true);
        trfBtn && (trfBtn.disabled = true);
      } else {
        storedRow(i).matched = false;
        tr.classList.remove('table-info');
        const resolved = rowIsResolved(i);
        cb && (cb.disabled = resolved);
        splitBtn && (splitBtn.disabled = resolved);
        trfBtn && (trfBtn.disabled = resolved);
        if (quick) {
          quick.disabled = rowIsResolved(i);
          updateQuickAssignPlaceholderState(quick);
        }
      }
      updatePredictionButtonStyle(i);
    }

    function clearManualSelectPointingTo(i) {
      document.querySelectorAll('.man-match-exp, .man-match-inc').forEach(sel => {
        if (sel.value === String(i)) sel.value = '';
      });
    }

    function clearMatch(i) {
      const { tx, src } = ensureHiddenMatchInputs(i);
      if (tx) tx.value = '';
      if (src) src.value = '';
      setMatchedState(i, false);
      clearManualSelectPointingTo(i);
    }

    const modalEl = document.getElementById('amountDiffModal');
    const bsModal = modalEl && window.bootstrap ? new bootstrap.Modal(modalEl) : null;
    const impAmtEl = document.getElementById('impAmt');
    const manAmtEl = document.getElementById('manAmt');
    const diffRowIndex = document.getElementById('diffRowIndex');
    let pending = null;

    function beginMatchFromManual(sel, section) {
      const manualId = parseInt(sel.getAttribute('data-manual-id') || '0', 10) || 0;
      const manualCents = parseInt(sel.getAttribute('data-manual-amount') || '0', 10) || 0;
      const iStr = sel.value;
      if (!iStr) return;
      const importIdx = parseInt(iStr, 10);
      const current = collectImports();
      const rec = (section === 'exp' ? current.exp : current.inc).find(r => r.i === importIdx);
      if (!rec) return;

      const { tx, src } = ensureHiddenMatchInputs(importIdx);
      if (!tx || !src) return;

      if (rec.cents !== manualCents && bsModal) {
        pending = { importIdx, manualId, importCents: rec.cents, manualCents, tx, src, selectEl: sel };
        impAmtEl.textContent = money(rec.cents);
        manAmtEl.textContent = money(manualCents);
        diffRowIndex.value = String(importIdx);
        bsModal.show();
      } else {
        tx.value = String(manualId);
        src.value = 'import';
        setMatchedState(importIdx, true);
      }
    }

    document.addEventListener('change', (e) => {
      const selExp = e.target.closest('.man-match-exp');
      if (selExp) { beginMatchFromManual(selExp, 'exp'); return; }
      const selInc = e.target.closest('.man-match-inc');
      if (selInc) { beginMatchFromManual(selInc, 'inc'); return; }
    });
    document.addEventListener('click', (e) => {
      const btn = e.target.closest('.man-clear');
      if (!btn) return;
      const manualId = btn.getAttribute('data-manual-id');
      const sel = document.querySelector(`.man-match-exp[data-manual-id="${CSS.escape(manualId)}"], .man-match-inc[data-manual-id="${CSS.escape(manualId)}"]`);
      if (sel) {
        const prev = sel.value;
        sel.value = '';
        if (prev) clearMatch(parseInt(prev,10));
      }
    });

    document.addEventListener('click', (e) => {
      const splitClear = e.target.closest('[data-role="split-clear"]');
      if (splitClear) {
        if (activeLazy?.type === 'split') clearSplitModalDraft(activeLazy.rowIndex);
        return;
      }
      const transferClear = e.target.closest('[data-role="transfer-clear"]');
      if (transferClear) {
        if (activeLazy?.type === 'transfer') clearTransferModalDraft(activeLazy.rowIndex);
        return;
      }
    });

    document.addEventListener('click', (e) => {
      const done = e.target.closest('[data-validate-env-dismiss]');
      const modal = done?.closest?.('#importSplitModal, #importTransferModal');
      if (!modal || !activeLazy) return;
      const rowIndex = String(activeLazy.rowIndex);
      const hasActiveConfig = activeLazy.type === 'split'
        ? rowHasAnySplitConfig(rowIndex)
        : rowHasAnyTransferConfig(rowIndex);
      modalDoneRowToCheck = {
        type: activeLazy.type,
        rowIndex,
      };
      if (hasActiveConfig) autoCheckImportRow(rowIndex);
    });

    modalEl?.addEventListener('click', (e) => {
      if (!pending) return;
      const t = e.target;
      const idx = pending.importIdx;

      if (t.id === 'cancelDiff') {
        if (pending.selectEl) pending.selectEl.value = '';
        clearMatch(idx);
        pending = null;
        bsModal.hide();
        return;
      }
      if (t.hasAttribute('data-accept')) {
        const srcChoice = t.getAttribute('data-accept');
        pending.tx.value = String(pending.manualId);
        pending.src.value = srcChoice || 'import';
        setMatchedState(idx, true);
        pending = null;
        bsModal.hide();
      }
    });

    const splitModal = document.getElementById('importSplitModal');
    const transferModal = document.getElementById('importTransferModal');
    document.addEventListener('pointerover', (e) => {
      const btn = e.target.closest?.('.exp-split-btn, .inc-split-btn, .trf-btn');
      if (btn) showActionPreview(btn);
      const source = e.target.closest?.('.import-payee-hover');
      if (source) showSourceTooltip(source);
    });
    document.addEventListener('pointerout', (e) => {
      const btn = e.target.closest?.('.exp-split-btn, .inc-split-btn, .trf-btn');
      if (btn && !btn.contains(e.relatedTarget)) hideActionPreview();
      const source = e.target.closest?.('.import-payee-hover');
      if (source && !source.contains(e.relatedTarget)) hideSourceTooltip();
    });
    document.addEventListener('focusin', (e) => {
      if (!actionPreviewFocusEnabled()) return;
      const btn = e.target.closest?.('.exp-split-btn, .inc-split-btn, .trf-btn');
      if (btn) showActionPreview(btn);
    });
    document.addEventListener('focusout', (e) => {
      if (!actionPreviewFocusEnabled()) return;
      if (e.target.closest?.('.exp-split-btn, .inc-split-btn, .trf-btn')) hideActionPreview();
    });
    window.addEventListener('scroll', hideActionPreview, true);
    window.addEventListener('scroll', hideSourceTooltip, true);
    window.addEventListener('resize', hideActionPreview);
    window.addEventListener('resize', hideSourceTooltip);
    splitModal?.addEventListener('show.bs.modal', (e) => {
      hideActionPreview();
      persistModal(splitModal);
      clearModalBody(splitModal);
      renderSplitModal(e.relatedTarget);
    });
    splitModal?.addEventListener('hidden.bs.modal', () => {
      const rowIndex = activeLazy?.rowIndex;
      persistModal(splitModal);
      const hasSplitConfig = rowIndex != null && rowHasAnySplitConfig(rowIndex);
      if (rowIndex != null) updateSplitButtonStyle(rowIndex, hasSplitConfig);
      if (
        hasSplitConfig
        && modalDoneRowToCheck?.type === 'split'
        && modalDoneRowToCheck.rowIndex === String(rowIndex)
      ) {
        autoCheckImportRow(rowIndex);
      }
      modalDoneRowToCheck = null;
      clearModalBody(splitModal);
    });
    transferModal?.addEventListener('show.bs.modal', (e) => {
      hideActionPreview();
      persistModal(transferModal);
      clearModalBody(transferModal);
      renderTransferModal(e.relatedTarget);
    });
    transferModal?.addEventListener('hidden.bs.modal', () => {
      const rowIndex = activeLazy?.rowIndex;
      persistModal(transferModal);
      const hasTransferConfig = rowIndex != null && rowHasAnyTransferConfig(rowIndex);
      if (rowIndex != null) updateTransferButtonStyle(rowIndex, hasTransferConfig);
      if (
        hasTransferConfig
        && modalDoneRowToCheck?.type === 'transfer'
        && modalDoneRowToCheck.rowIndex === String(rowIndex)
      ) {
        autoCheckImportRow(rowIndex);
      }
      modalDoneRowToCheck = null;
      clearModalBody(transferModal);
    });
    reviewForm.addEventListener('input', (e) => {
      if (e.target.closest('#importDraftRestoreModal')) return;
      if (e.target.matches('.import-payee-input')) syncPayeeTooltip(e.target);
      scheduleDraftSave();
    });
    reviewForm.addEventListener('change', (e) => {
      if (e.target.closest('#importDraftRestoreModal')) return;
      scheduleDraftSave();
    });
    reviewForm.addEventListener('click', (e) => {
      if (e.target.closest('#importDraftRestoreModal')) return;
      if (e.target.closest('.man-clear, .predict-clear-btn, [data-role="split-clear"], [data-role="transfer-clear"], .env-default-btn')) {
        scheduleDraftSave();
      }
    });
    refreshDraftFingerprintInput();
    window.addEventListener('beforeunload', flushDraftSave);
    reviewForm.addEventListener('submit', () => {
      persistModal(splitModal);
      persistModal(transferModal);
      clearModalBody(splitModal);
      clearModalBody(transferModal);
      pruneUncheckedRowsForCommit();
      flushDraftSave();
    });

    accountSel?.addEventListener('change', async () => {
      const nextAccountId = String(accountSel?.value || '');
      persistModal(splitModal);
      persistModal(transferModal);
      if (nextAccountId !== lastAccountId && hasAnyCreationState() && !window.confirm('Changing accounts clears import predictions and draft assignments. Continue?')) {
        accountSel.value = lastAccountId;
        return;
      }
      clearModalBody(splitModal);
      clearModalBody(transferModal);
      if (nextAccountId !== initialPrefillAccountId) {
        clearAppliedImportPrefills();
        clearAppliedPayeeNormalizations();
      }
      lastAccountId = nextAccountId;
      refreshDraftFingerprintInput();
      filterQuickAssign();
      await refreshDupes();
      const refreshed = collectImports();
      populateManualSelects('.man-match-exp', refreshed.exp);
      populateManualSelects('.man-match-inc', refreshed.inc);
      fetchAndRenderManual();
    });

    function amountInputValue(cents, options = {}) {
      const parsed = parseInt(cents, 10) || 0;
      const forcePositive = !!options.forcePositive;
      const sign = parsed < 0 && !forcePositive ? '-' : '';
      return sign + (Math.abs(parsed) / 100).toFixed(2);
    }

    function clearCreationStateForRow(rowIndex, options = {}) {
      const idx = String(rowIndex);
      const tr = rowFor(idx);
      const quick = tr?.querySelector(`select[name="exp_single_${CSS.escape(idx)}"]`);
      if (quick) {
        quick.value = '';
        updateQuickAssignPlaceholderState(quick);
      }
      if (lazyState.delete(idx)) renderHiddenFields();
      if (options.discardPredictionFeedback) {
        predictionFeedbackRows.delete(idx);
      } else if (predictionFeedbackRows.has(idx)) {
        const item = { ...predictionFeedbackRows.get(idx), status: 'cleared' };
        predictionFeedbackRows.set(idx, item);
      }
      prefilledRows.delete(idx);
      updateSplitButtonStyle(idx, false);
      updateTransferButtonStyle(idx, false);
      updatePredictionButtonStyle(idx);
      renderHiddenFields();
    }

    function rowHasDraftChoices(rowIndex) {
      const idx = String(rowIndex);
      const tr = rowFor(idx);
      const quick = tr?.querySelector(`select[name="exp_single_${CSS.escape(idx)}"]`);
      return !!(quick?.value || lazyState.has(idx));
    }

    function hasAnyCreationState() {
      if (lazyState.size > 0) return true;
      return Array.from(document.querySelectorAll('tr[data-row-index]')).some((tr) => rowHasDraftChoices(tr.getAttribute('data-row-index')));
    }

    function updatePredictionButtonStyle(rowIndex) {
      const idx = String(rowIndex);
      const tr = rowFor(idx);
      if (!tr) return;
      const btn = tr.querySelector('.predict-clear-btn');
      if (!btn) return;
      const show = prefilledRows.has(idx)
        && (rowHasAnySplitConfig(idx) || rowHasAnyTransferConfig(idx))
        && !rowIsResolved(idx);
      btn.hidden = !show;
      btn.disabled = !show;
    }

    function rowIsLockedForExistingTransaction(rowIndex) {
      const tr = rowFor(rowIndex);
      if (!tr) return true;
      return rowIsResolved(rowIndex);
    }

    function splitPrefixForRow(rowIndex, prefill) {
      const tr = rowFor(rowIndex);
      const amount = parseInt(tr?.dataset.amountCents || '0', 10) || 0;
      const type = String(prefill?.transaction_type || '').toLowerCase();
      if (type === 'expense' || amount < 0) return 'exp';
      if (type === 'income' || amount > 0) return 'inc';
      return null;
    }

    function seedSplitPrefill(rowIndex, prefix, splits) {
      let seeded = false;
      (splits || []).forEach((split) => {
        const envelopeId = parseInt(split.envelope_id, 10) || 0;
        const amountCents = parseInt(split.amount_cents, 10) || 0;
        if (!envelopeId || !amountCents) return;
        setLazyField(rowIndex, `${prefix}_amount_${rowIndex}_${envelopeId}`, amountInputValue(amountCents));
        seeded = true;
      });
      return seeded;
    }

    function envelopeExists(envelopeId) {
      const value = String(parseInt(envelopeId, 10) || 0);
      return value !== '0' && envelopes.some((envelope) => String(envelope.id) === value);
    }

    function seedRemainderPrefill(rowIndex, fieldName, envelopeId) {
      if (!envelopeExists(envelopeId)) return false;
      setLazyField(rowIndex, fieldName, String(parseInt(envelopeId, 10)));
      return true;
    }

    function seedSingleIncomePrefill(rowIndex, envelopeId) {
      const tr = rowFor(rowIndex);
      const amountCents = parseInt(tr?.dataset.amountCents || '0', 10) || 0;
      if (!envelopeId || amountCents <= 0) return false;
      return seedSplitPrefill(rowIndex, 'inc', [{ envelope_id: envelopeId, amount_cents: amountCents }]);
    }

    function seedSingleExpensePrefill(rowIndex, envelopeId) {
      const tr = rowFor(rowIndex);
      const quick = tr?.querySelector(`select[name="exp_single_${CSS.escape(String(rowIndex))}"]`);
      if (!quick || quick.disabled || !envelopeId) return false;
      const value = String(envelopeId);
      const opt = Array.from(quick.options).find((option) => option.value === value);
      if (!opt || opt.hidden || opt.disabled) return false;
      quick.value = value;
      updateQuickAssignPlaceholderState(quick);
      updatePredictionButtonStyle(rowIndex);
      return true;
    }

    function accountExistsForTransfer(accountId) {
      const value = String(accountId || '');
      if (!value) return false;
      const currentAccount = String(accountSel?.value || '');
      return value !== currentAccount && accounts.some((account) => String(account.id) === value);
    }

    function clearQuickAssign(rowIndex) {
      const tr = rowFor(rowIndex);
      const quick = tr?.querySelector(`select[name="exp_single_${CSS.escape(String(rowIndex))}"]`);
      if (quick) {
        quick.value = '';
        updateQuickAssignPlaceholderState(quick);
      }
      updatePredictionButtonStyle(rowIndex);
    }

    function seedTransferLeg(rowIndex, prefix, splits, options = {}) {
      let seeded = false;
      (splits || []).forEach((split) => {
        const envelopeId = parseInt(split.envelope_id, 10) || 0;
        const amountCents = parseInt(split.amount_cents, 10) || 0;
        if (!envelopeId || !amountCents) return;
        setLazyField(rowIndex, `${prefix}_${rowIndex}_${envelopeId}`, amountInputValue(amountCents, options));
        seeded = true;
      });
      return seeded;
    }

    function seedTransferPrefill(rowIndex, transfer) {
      const otherAccountId = parseInt(transfer?.other_account_id, 10) || 0;
      if (!accountExistsForTransfer(otherAccountId)) return false;

      clearQuickAssign(rowIndex);
      clearLazyFieldsForRow(rowIndex, splitFieldMatchers(rowIndex));
      setLazyField(rowIndex, `is_transfer_${rowIndex}`, '1');
      setLazyField(rowIndex, `transfer_account_${rowIndex}`, String(otherAccountId));
      const input = payeeInput(rowIndex);
      const original = originalPayeeInput(rowIndex)?.value || '';
      const current = input?.value || '';
      const otherAccountName = String(transfer.other_account_name || '').trim();
      if (input && !input.disabled && otherAccountName && current === original && current !== otherAccountName) {
        input.value = otherAccountName;
        syncPayeeTooltip(input);
        payeeNormalizedRows.add(String(rowIndex));
      }
      const seededCurrent = seedTransferLeg(rowIndex, 'trf_from_amt', transfer.current_account_splits || [], { forcePositive: true });
      const seededCurrentRemainder = seedRemainderPrefill(
        rowIndex,
        `trf_from_remainder_${rowIndex}`,
        transfer.current_account_remainder_envelope_id
      );
      const seededOther = seedTransferLeg(rowIndex, 'trf_amt', transfer.other_account_splits || []);
      const seededOtherRemainder = seedRemainderPrefill(
        rowIndex,
        `trf_remainder_${rowIndex}`,
        transfer.other_account_remainder_envelope_id
      );
      return seededCurrent || seededCurrentRemainder || seededOther || seededOtherRemainder;
    }

    function cleanPredictionSplits(splits) {
      return (Array.isArray(splits) ? splits : []).map((split) => ({
        envelope_id: parseInt(split.envelope_id, 10) || 0,
        amount_cents: parseInt(split.amount_cents, 10) || 0,
      })).filter((split) => split.envelope_id && split.amount_cents)
        .sort((a, b) => (a.envelope_id - b.envelope_id) || (a.amount_cents - b.amount_cents));
    }

    function normalizedPredictionJson(prefill) {
      const transfer = prefill?.transfer || null;
      const normalized = {
        prediction_type: prefill?.prediction_type || 'new_transaction',
        transaction_type: prefill?.transaction_type || null,
        single_envelope_id: parseInt(prefill?.single_envelope_id, 10) || null,
        splits: cleanPredictionSplits(prefill?.splits),
        remainder_envelope_id: parseInt(prefill?.remainder_envelope_id, 10) || null,
        remainder_amount_cents: prefill?.remainder_amount_cents ?? null,
        transfer: null,
      };
      if (transfer) {
        normalized.transfer = {
          other_account_id: parseInt(transfer.other_account_id, 10) || null,
          current_account_splits: cleanPredictionSplits(transfer.current_account_splits),
          current_account_remainder_envelope_id: parseInt(transfer.current_account_remainder_envelope_id, 10) || null,
          current_account_remainder_amount_cents: transfer.current_account_remainder_amount_cents ?? null,
          other_account_splits: cleanPredictionSplits(transfer.other_account_splits),
          other_account_remainder_envelope_id: parseInt(transfer.other_account_remainder_envelope_id, 10) || null,
          other_account_remainder_amount_cents: transfer.other_account_remainder_amount_cents ?? null,
        };
      }
      return normalized;
    }

    function rememberPredictionFeedback(rowIndex, prefill) {
      const idx = String(rowIndex);
      const debug = prefill?.prediction_debug || {};
      const evidence = debug.evidence || {};
      predictionFeedbackRows.set(idx, {
        row_index: parseInt(idx, 10) || 0,
        prediction_id: prefill?.prediction_id || `import-review:${initialPrefillAccountId}:${importDraftIdentity.fingerprint || ''}:${idx}:${evidence.learning_example_id || ''}:${debug.score || ''}`,
        prediction_type: prefill?.prediction_type || debug.prediction_type || 'new_transaction',
        learning_example_id: parseInt(evidence.learning_example_id, 10) || null,
        predicted_json: normalizedPredictionJson(prefill),
        status: 'applied',
      });
      renderHiddenFields();
    }


    function originalPayeeInput(rowIndex) {
      const tr = rowFor(rowIndex);
      return tr?.querySelector(`input[name="orig_payee_${CSS.escape(String(rowIndex))}"]`);
    }

    function payeeInput(rowIndex) {
      const tr = rowFor(rowIndex);
      return tr?.querySelector(`input[name="payee_${CSS.escape(String(rowIndex))}"]`);
    }

    function originalMemoInput(rowIndex) {
      const tr = rowFor(rowIndex);
      return tr?.querySelector(`input[name="orig_memo_${CSS.escape(String(rowIndex))}"]`);
    }

    function memoInput(rowIndex) {
      const tr = rowFor(rowIndex);
      return tr?.querySelector(`input[name="memo_${CSS.escape(String(rowIndex))}"]`);
    }

    function applyPayeeNormalizationPrefills() {
      (importPayeePrefills || []).forEach((prefill) => {
        if (!prefill || (!prefill.payee_prefill && !prefill.memo_prefill)) return;
        const rowIndex = String(prefill.row_index);
        if (rowIsLockedForExistingTransaction(rowIndex)) return;
        if (prefill.payee_prefill) {
          const input = payeeInput(rowIndex);
          const original = originalPayeeInput(rowIndex)?.value || '';
          const current = input?.value || '';
          const canonical = String(prefill.canonical_payee || '').trim();
          if (input && !input.disabled && canonical && current === original && canonical !== current) {
            input.value = canonical;
            syncPayeeTooltip(input);
            payeeNormalizedRows.add(rowIndex);
          }
        }
        if (prefill.memo_prefill) {
          const input = memoInput(rowIndex);
          const original = originalMemoInput(rowIndex)?.value || '';
          const current = input?.value || '';
          const canonical = String(prefill.canonical_memo ?? '');
          if (input && !input.disabled && current === original && canonical !== current) {
            input.value = canonical;
            memoNormalizedRows.add(rowIndex);
          }
        }
      });
    }

    function clearAppliedPayeeNormalizations() {
      Array.from(payeeNormalizedRows).forEach((rowIndex) => {
        const input = payeeInput(rowIndex);
        const original = originalPayeeInput(rowIndex)?.value || '';
        if (input && !input.disabled) {
          input.value = original;
          syncPayeeTooltip(input);
        }
      });
      payeeNormalizedRows.clear();
      Array.from(memoNormalizedRows).forEach((rowIndex) => {
        const input = memoInput(rowIndex);
        const original = originalMemoInput(rowIndex)?.value || '';
        if (input && !input.disabled) {
          input.value = original;
        }
      });
      memoNormalizedRows.clear();
    }

    function applyImportPrefills() {
      let changed = false;
      (importPrefills || []).forEach((prefill) => {
        if (!prefill || !prefill.prefill) return;
        const rowIndex = String(prefill.row_index);
        if (rowIsLockedForExistingTransaction(rowIndex)) return;

        let applied = false;
        if (prefill.transfer) {
          applied = seedTransferPrefill(rowIndex, prefill.transfer);
        } else {
          const prefix = splitPrefixForRow(rowIndex, prefill);
          if (!prefix) return;
          const singleEnvelopeId = parseInt(prefill.single_envelope_id, 10) || 0;
          const splits = Array.isArray(prefill.splits) ? prefill.splits : [];
          const remainderEnvelopeId = parseInt(prefill.remainder_envelope_id, 10) || 0;
          if (remainderEnvelopeId) {
            clearQuickAssign(rowIndex);
            const seededSplits = splits.length ? seedSplitPrefill(rowIndex, prefix, splits) : false;
            const seededRemainder = seedRemainderPrefill(rowIndex, `${prefix}_remainder_${rowIndex}`, remainderEnvelopeId);
            applied = seededSplits || seededRemainder;
          } else if (singleEnvelopeId && prefix === 'exp') {
            applied = seedSingleExpensePrefill(rowIndex, singleEnvelopeId);
          } else if (singleEnvelopeId && prefix === 'inc') {
            applied = seedSingleIncomePrefill(rowIndex, singleEnvelopeId);
          } else if (splits.length) {
            clearQuickAssign(rowIndex);
            applied = seedSplitPrefill(rowIndex, prefix, splits);
          }
        }

        if (applied) {
          prefilledRows.add(rowIndex);
          rememberPredictionFeedback(rowIndex, prefill);
          updateSplitButtonStyle(rowIndex, rowHasAnySplitConfig(rowIndex));
          updateTransferButtonStyle(rowIndex, rowHasAnyTransferConfig(rowIndex));
          updatePredictionButtonStyle(rowIndex);
          changed = true;
        }
      });
      if (changed) renderHiddenFields();
    }

    function clearAppliedImportPrefills() {
      Array.from(prefilledRows).forEach((rowIndex) => clearCreationStateForRow(rowIndex, { discardPredictionFeedback: true }));
      prefilledRows.clear();
      predictionFeedbackRows.clear();
      renderHiddenFields();
    }

    document.addEventListener('change', (e) => {
      const quick = e.target.closest('select[data-role="exp-quick"]');
      if (!quick) return;
      if (quick.value) autoCheckImportRow(quick.dataset.rowIndex);
      updatePredictionButtonStyle(quick.dataset.rowIndex);
    });

    document.addEventListener('click', (e) => {
      const btn = e.target.closest('.predict-clear-btn');
      if (!btn) return;
      clearCreationStateForRow(btn.dataset.rowIndex);
    });

    function updateTransferButtonStyle(rowIndex, isActive) {
      const tr = rowFor(rowIndex);
      if (!tr) return;
      const btn = tr.querySelector('.trf-btn');
      if (!btn) return;
      btn.classList.toggle('btn-outline-primary', !isActive);
      btn.classList.toggle('btn-primary', isActive);
      updatePredictionButtonStyle(rowIndex);
    }

    function hasNonZeroValue(value) {
      const val = (value || '').trim();
      return !!val && val !== '0' && val !== '0.00';
    }

    function splitFieldMatchers(rowIndex) {
      const idx = String(rowIndex);
      return [
        new RegExp(`^(exp|inc)_amount_${idx}_`),
        new RegExp(`^(exp|inc)_remainder_${idx}$`),
      ];
    }

    function transferFieldMatchers(rowIndex) {
      const idx = String(rowIndex);
      return [
        new RegExp(`^is_transfer_${idx}$`),
        new RegExp(`^transfer_account_${idx}$`),
        new RegExp(`^trf_amt_${idx}_`),
        new RegExp(`^trf_remainder_${idx}$`),
        new RegExp(`^trf_from_amt_${idx}_`),
        new RegExp(`^trf_from_remainder_${idx}$`),
      ];
    }

    function clearFieldsByMatchers(fields, matchers) {
      Object.keys(fields).forEach((name) => {
        if (matchers.some((re) => re.test(name))) delete fields[name];
      });
    }

    function clearSplitFields(fields, rowIndex) {
      clearFieldsByMatchers(fields, splitFieldMatchers(rowIndex));
    }

    function clearTransferFields(fields, rowIndex) {
      clearFieldsByMatchers(fields, transferFieldMatchers(rowIndex));
    }

    function rowHasAnyTransferConfig(rowIndex, fields = rowState(rowIndex)) {
      const idx = String(rowIndex);
      for (const [name, value] of Object.entries(fields)) {
        if ((name.startsWith(`trf_amt_${idx}_`) || name.startsWith(`trf_from_amt_${idx}_`)) && hasNonZeroValue(value)) return true;
        if ((name === `trf_remainder_${idx}` || name === `trf_from_remainder_${idx}`) && hasNonZeroValue(value)) return true;
      }
      const modal = activeLazy?.type === 'transfer' && activeLazy.rowIndex === idx ? transferModal : null;
      if (modal) {
        for (const input of modal.querySelectorAll(`input[name^="trf_amt_${idx}_"], input[name^="trf_from_amt_${idx}_"]`)) {
          if (hasNonZeroValue(input.value)) return true;
        }
        for (const sel of modal.querySelectorAll(`select[name="trf_remainder_${idx}"], select[name="trf_from_remainder_${idx}"]`)) {
          if (hasNonZeroValue(sel.value)) return true;
        }
      }
      return false;
    }

    function updateSplitButtonStyle(rowIndex, isActive) {
      const tr = rowFor(rowIndex);
      if (!tr) return;
      const btn = tr.querySelector('.exp-split-btn, .inc-split-btn');
      if (!btn) return;
      btn.classList.toggle('btn-outline-secondary', !isActive);
      btn.classList.toggle('btn-secondary', isActive);
      updatePredictionButtonStyle(rowIndex);
    }

    function rowHasAnySplitConfig(rowIndex, fields = rowState(rowIndex)) {
      const idx = String(rowIndex);
      for (const [name, value] of Object.entries(fields)) {
        if ((name.startsWith(`exp_amount_${idx}_`) || name.startsWith(`inc_amount_${idx}_`)) && hasNonZeroValue(value)) return true;
        if ((name === `exp_remainder_${idx}` || name === `inc_remainder_${idx}`) && hasNonZeroValue(value)) return true;
      }
      const modal = activeLazy?.type === 'split' && activeLazy.rowIndex === idx ? splitModal : null;
      if (modal) {
        for (const input of modal.querySelectorAll(`input[name^="exp_amount_${idx}_"], input[name^="inc_amount_${idx}_"]`)) {
          if (hasNonZeroValue(input.value)) return true;
        }
        for (const sel of modal.querySelectorAll(`select[name="exp_remainder_${idx}"], select[name="inc_remainder_${idx}"]`)) {
          if (hasNonZeroValue(sel.value)) return true;
        }
      }
      return false;
    }

    (async function init(){
      await refreshDupes();
      syncAllPayeeTooltips();
      applyImportPrefills();
      applyPayeeNormalizationPrefills();
      const refreshed = collectImports();
      populateManualSelects('.man-match-exp', refreshed.exp);
      populateManualSelects('.man-match-inc', refreshed.inc);
      await fetchAndRenderManual();
      showDraftRestoreModalIfNeeded();
      document.querySelectorAll('tr[data-row-index]').forEach(tr => {
        const idx = tr.getAttribute('data-row-index');
        updateSplitButtonStyle(idx, rowHasAnySplitConfig(idx));
        updateTransferButtonStyle(idx, rowHasAnyTransferConfig(idx));
        updatePredictionButtonStyle(idx);
      });
    })();
  });
})();
