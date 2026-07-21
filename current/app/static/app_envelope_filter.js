// static/app_envelope_filter.js
// Account-aware envelope selector with:
//  - DOM rebuild from templates (no hidden rows)
//  - "Remainder to" select mirrors visible envelopes
//  - Optional Remaining readout when a total input is supplied
//  - "Default" button per row (income) to fill from envelope default
//  - **Per-account balances** shown in each row (hide when zero)

(function () {
  const READY = (fn) =>
    document.readyState === "loading"
      ? document.addEventListener("DOMContentLoaded", fn, { once: true })
      : fn();

  // --- helpers ---
  const toStr = (v) => (v == null ? "" : String(v).trim());
  const money = (cents) => {
    const n = (parseInt(cents, 10) || 0) / 100;
    const sign = n < 0 ? "-" : "";
    const abs = Math.abs(n);
    return `${sign}$${abs.toFixed(2)}`;
  };

  // Pull balances JSON from <script type="application/json" id="balances-json"> … </script>
  // Fallback to window.balances_json if you already expose it elsewhere.
  function getBalancesJSON() {
    try {
      const el = document.getElementById("balances-json");
      if (el && el.textContent) return JSON.parse(el.textContent);
    } catch {}
    return (window.balances_json || {}); // shape: { [account_id]: { [envelope_id]: cents } }
  }

  function getList(scopeEl) { return scopeEl.querySelector("[data-env-list]"); }
  function getRows(scopeEl) { return Array.from(scopeEl.querySelectorAll("[data-env-row]")); }
  function normalizeLocked(v) {
    const s = toStr(v);
    return (s === "" || s.toLowerCase() === "null" || s.toLowerCase() === "none") ? "" : s;
  }

  function captureTemplates(scopeEl) {
    const list = getList(scopeEl);
    const rows = Array.from(list.querySelectorAll("[data-env-row]"));
    scopeEl.__envRowTemplates = rows.map((row) => ({
      eid: toStr(row.getAttribute("data-envelope-id")),
      name: toStr(row.getAttribute("data-envelope-name")),
      locked: normalizeLocked(row.getAttribute("data-locked-account-id")),
      html: row.outerHTML,
    }));
    rows.forEach((r) => r.remove());
  }

  function accountSelect(scopeEl) {
    const selStr = scopeEl.getAttribute("data-account-selector");
    if (!selStr) return null;
    const form = scopeEl.closest("form");
    return form ? form.querySelector(selStr) : null;
  }
  function safeAccountValue(sel) {
    if (!sel) return "";
    let val = toStr(sel.value);
    if (!val) {
      const idx = sel.selectedIndex >= 0 ? sel.selectedIndex : 0;
      if (sel.options && sel.options.length > idx) {
        val = toStr(sel.options[idx].value);
      }
    }
    return val;
  }

  function captureValues(scopeEl) {
    const out = {};
    getRows(scopeEl).forEach((row) => {
      const eid = toStr(row.getAttribute("data-envelope-id"));
      if (!eid) return;
      const amtEl = row.querySelector("input.env-input");
      const modeEl = row.querySelector("select.env-mode");
      out[eid] = { amt: amtEl ? toStr(amtEl.value) : "", mode: modeEl ? toStr(modeEl.value) : "" };
    });
    return out;
  }
  function restoreValues(rowEl, saved) {
    const eid = toStr(rowEl.getAttribute("data-envelope-id"));
    if (!eid || !saved || !saved[eid]) return;
    const { amt, mode } = saved[eid];
    const amtEl = rowEl.querySelector("input.env-input");
    const modeEl = rowEl.querySelector("select.env-mode");
    if (amtEl && amt !== "") amtEl.value = amt;
    if (modeEl && mode !== "") modeEl.value = mode;
  }

  function updateBalances(scopeEl, accId) {
    const balances = getBalancesJSON();
    const acctMap = balances && balances[accId] ? balances[accId] : null;

    getRows(scopeEl).forEach((row) => {
      const eid = row.getAttribute("data-envelope-id");
      const wrap = row.querySelector("[data-balance-wrap]");
      const span = row.querySelector("[data-env-balance]");
      if (!wrap || !span) return;

      const cents = acctMap && acctMap[eid] != null ? acctMap[eid] : 0;
      const showZeroBalance = truthyData(wrap.getAttribute("data-show-zero-balance"));
      if (!cents && !showZeroBalance) {
        wrap.hidden = true; // hide when zero
      } else {
        wrap.hidden = false;
        span.textContent = money(cents);
        span.classList.toggle("text-danger", cents < 0);
      }
    });
  }

  function rebuild(scopeEl) {
    const list = getList(scopeEl);
    if (!list || !scopeEl.__envRowTemplates) return;

    const sel = accountSelect(scopeEl);
    const accId = safeAccountValue(sel);
    const saved = captureValues(scopeEl);

    // Clear and render allowed rows
    list.innerHTML = "";
    const tmpls = scopeEl.__envRowTemplates;
    const allowed = accId === "" ? tmpls : tmpls.filter((t) => t.locked === "" || t.locked === accId);

    const frag = document.createDocumentFragment();
    allowed.forEach((t) => {
      const wrap = document.createElement("div");
      wrap.innerHTML = t.html;
      const rowEl = wrap.firstElementChild;
      rowEl.querySelectorAll("input, select, button.env-default-btn").forEach((el) => (el.disabled = false));
      restoreValues(rowEl, saved);
      frag.appendChild(rowEl);
    });
    list.appendChild(frag);

    populateRemainder(scopeEl, allowed);
    wireRemaining(scopeEl);
    wireDefaults(scopeEl);
    updateBalances(scopeEl, accId || ""); // <-- per-account balances
  }

  function remainderTemplates(scopeEl, allowedTemplates) {
    const sel = accountSelect(scopeEl);
    const accId = safeAccountValue(sel);
    const blankMode = toStr(scopeEl.getAttribute("data-blank-account-remainder-mode")).toLowerCase();
    if (accId === "" && blankMode === "global" && scopeEl.__envRowTemplates) {
      return scopeEl.__envRowTemplates.filter((t) => t.locked === "");
    }
    return allowedTemplates;
  }

  function populateRemainder(scopeEl, allowedTemplates) {
    const rem = scopeEl.querySelector("[data-remainder-select]");
    if (!rem) return;

    const remainderAllowed = remainderTemplates(scopeEl, allowedTemplates);
    const prev = toStr(rem.value);
    const preseedEl = scopeEl.querySelector("[data-remainder-initial]");
    const preseed = preseedEl ? toStr(preseedEl.value) : "";

    rem.innerHTML = "";
    const optNone = document.createElement("option");
    optNone.value = "";
    optNone.textContent = "-- none --";
    rem.appendChild(optNone);

    let hasPrev = false;

    remainderAllowed.forEach((t) => {
      const opt = document.createElement("option");
      opt.value = t.eid;
      opt.textContent = t.name || `Envelope ${t.eid}`;
      if (prev && t.eid === prev) { opt.selected = true; hasPrev = true; }
      rem.appendChild(opt);
    });

    if (!hasPrev && preseed) {
      const candidate = rem.querySelector(`option[value="${CSS.escape(preseed)}"]`);
      if (candidate) candidate.selected = true;
    }
  }

  function parseDollarInput(el) {
    if (!el) return 0;
    const raw = String(el.value || "").replace(/,/g, "").trim();
    if (!raw) return 0;
    const n = Number(raw);
    if (!Number.isFinite(n)) return 0;
    // Return integer cents, rounded to nearest cent
    return Math.round(n * 100);
  }

  function truthyData(value) {
    const s = toStr(value).toLowerCase();
    return s === "1" || s === "true" || s === "yes";
  }

  function remainderTargetSign(scopeEl) {
    return toStr(scopeEl.getAttribute("data-remainder-target-sign")) === "-1" ? -1 : 1;
  }

  function usesLegacyOutflow(scopeEl) {
    return truthyData(scopeEl.getAttribute("data-remainder-legacy-outflow"));
  }

  function selectedRemainderValue(scopeEl) {
    const remSelect = scopeEl.querySelector("[data-remainder-select]");
    return remSelect ? toStr(remSelect.value) : "";
  }

  function currentBalanceCents(scopeEl, row) {
    const sel = accountSelect(scopeEl);
    const accId = safeAccountValue(sel);
    const eid = toStr(row.getAttribute("data-envelope-id"));
    const balances = getBalancesJSON();
    const acctMap = balances && accId ? balances[accId] : null;
    return acctMap && acctMap[eid] != null ? parseInt(acctMap[eid], 10) || 0 : 0;
  }

  function rowSplitCents(scopeEl, row) {
    const input = row.querySelector("input.env-input");
    if (!input || input.disabled) return 0;
    const enteredCents = parseDollarInput(input);
    const mode = toStr(row.querySelector("select.env-mode")?.value).toLowerCase();
    if (mode === "set") {
      return enteredCents - currentBalanceCents(scopeEl, row);
    }
    return enteredCents;
  }

  function enteredSplitCents(scopeEl) {
    return getRows(scopeEl)
      .map((row) => rowSplitCents(scopeEl, row))
      .filter((cents) => cents !== 0);
  }

  function signedTargetCents(scopeEl, totalCents) {
    return remainderTargetSign(scopeEl) < 0 ? -Math.abs(totalCents) : totalCents;
  }

  function signedEnteredTotalCents(scopeEl, values) {
    const targetIsOutflow = remainderTargetSign(scopeEl) < 0;
    const hasNegative = values.some((cents) => cents < 0);
    if (targetIsOutflow && usesLegacyOutflow(scopeEl) && !hasNegative) {
      return values.reduce((sum, cents) => sum - Math.abs(cents), 0);
    }
    return values.reduce((sum, cents) => sum + cents, 0);
  }

  function signedRemainderCents(scopeEl, totalCents, values) {
    return signedTargetCents(scopeEl, totalCents) - signedEnteredTotalCents(scopeEl, values);
  }

  function updateRemainderAmount(scopeEl, hasSelection, cents) {
    const amountEl = scopeEl.querySelector("[data-remainder-amount]");
    if (!amountEl) return;

    if (!hasSelection) {
      amountEl.textContent = "No remainder assigned";
      amountEl.classList.add("text-muted");
      amountEl.classList.remove("text-danger");
      return;
    }

    amountEl.textContent = money(cents);
    amountEl.classList.remove("text-muted");
    amountEl.classList.toggle("text-danger", cents < 0);
  }

  function wireRemaining(scopeEl) {
    const totalSel = scopeEl.getAttribute("data-total-selector");
    if (!totalSel) {
      updateRemainderAmount(scopeEl, false, 0);
      return;
    }

    const form = scopeEl.closest("form");
    const total = form ? form.querySelector(totalSel) : null;
    const remEl = scopeEl.querySelector("[data-remaining-amount]");
    if (!total || !remEl) {
      updateRemainderAmount(scopeEl, false, 0);
      return;
    }

    const recalc = () => {
      const totalCents = parseDollarInput(total);  // integer cents
      const values = enteredSplitCents(scopeEl);
      const displayRemainingCents = totalCents - values.reduce((sum, cents) => sum + cents, 0);
      const remainderCents = signedRemainderCents(scopeEl, totalCents, values);

      const hasRemainderSelection = selectedRemainderValue(scopeEl) !== "";
      const remainingCents = hasRemainderSelection ? 0 : displayRemainingCents;

      updateRemainderAmount(scopeEl, hasRemainderSelection, remainderCents);
      remEl.textContent = money(remainingCents);  // money() expects cents
      remEl.classList.toggle("text-danger", remainingCents < 0);
    };

    if (!scopeEl.__remainingBound) {
      scopeEl.__remainingBound = true;

      total.addEventListener("input", recalc, { passive: true });
      total.addEventListener("change", recalc, { passive: true });

      scopeEl.addEventListener(
        "input",
        (e) => {
          if (
            e.target &&
            e.target.classList &&
            e.target.classList.contains("env-input")
          ) {
            recalc();
          }
        },
        { passive: true }
      );

      scopeEl.addEventListener(
        "change",
        (e) => {
          if (e.target && e.target.matches("[data-remainder-select], select.env-mode")) {
            recalc();
          }
        },
        { passive: true }
      );
    }

    recalc();
  }


  function wireDefaults(scopeEl) {
    if (scopeEl.__defaultsBound) return;
    scopeEl.__defaultsBound = true;

    scopeEl.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-env-default]");
      if (!btn || !scopeEl.contains(btn)) return;

      const row = btn.closest("[data-env-row]");
      if (!row) return;

      const cents = parseInt(row.getAttribute("data-default-cents") || "0", 10) || 0;
      const dollars = (cents / 100).toFixed(2);

      const input = row.querySelector("input.env-input");
      if (input) {
        input.value = dollars;
        const evt = new Event("input", { bubbles: true });
        input.dispatchEvent(evt);
      }
    }, false);
  }

  function validationLabel(scopeEl) {
    const configured = toStr(scopeEl.getAttribute("data-validation-label"));
    if (configured) return configured;
    const scopeId = toStr(scopeEl.getAttribute("data-scope-id"));
    if (scopeId) return scopeId.replace(/[-_]+/g, " ");
    return "envelope amounts";
  }

  function validationState(scopeEl) {
    const toggleSel = toStr(scopeEl.getAttribute("data-validation-toggle"));
    if (toggleSel) {
      const modal = scopeEl.closest(".modal");
      const form = scopeEl.closest("form");
      const root = modal || form || document;
      const toggle = root.querySelector(toggleSel);
      if (toggle && !toggle.checked) return null;
    }

    const totalSel = scopeEl.getAttribute("data-total-selector");
    if (!totalSel) return null;
    const form = scopeEl.closest("form");
    const total = form ? form.querySelector(totalSel) : null;
    if (!total) return null;

    const totalCents = parseDollarInput(total);
    const values = enteredSplitCents(scopeEl);
    const signedRemaining = signedRemainderCents(scopeEl, totalCents, values);
    return {
      total,
      totalCents,
      values,
      signedRemaining,
      hasRemainderSelection: selectedRemainderValue(scopeEl) !== "",
    };
  }

  function formErrorBar(form) {
    let bar = form.querySelector('.modal-body .alert[data-role="form-error"]');
    if (!bar) {
      bar = document.createElement("div");
      bar.className = "alert alert-danger py-2";
      bar.setAttribute("data-role", "form-error");
      const body = form.querySelector(".modal-body") || form;
      body.prepend(bar);
    }
    return bar;
  }

  function showFormError(form, msg) {
    const bar = formErrorBar(form);
    bar.textContent = msg;
  }

  function clearFormError(form) {
    const bar = form.querySelector('.modal-body .alert[data-role="form-error"]');
    if (bar) bar.remove();
  }

  function focusFirstEditableAmount(scopeEl, fallback) {
    const input = getRows(scopeEl)
      .map((row) => row.querySelector("input.env-input"))
      .find((el) => el && !el.disabled && !el.readOnly);
    (input || fallback)?.focus();
  }

  function validateEnvelopeScope(scopeEl) {
    const state = validationState(scopeEl);
    if (!state) return { ok: true };

    if (state.signedRemaining !== 0 && !state.hasRemainderSelection) {
      return {
        ok: false,
        state,
        message:
          `The ${validationLabel(scopeEl)} has ${money(Math.abs(state.signedRemaining))} remaining. ` +
          `Choose a remainder envelope or adjust the amounts.`,
      };
    }

    return { ok: true, state };
  }

  function wireFormValidation(scopeEl) {
    const form = scopeEl.closest("form");
    if (!form || form.__envValidationBound) return;
    form.__envValidationBound = true;

    form.addEventListener("submit", (e) => {
      const scopes = Array.from(form.querySelectorAll(".env-scope[data-total-selector]"));
      for (const scope of scopes) {
        const result = validateEnvelopeScope(scope);
        if (!result.ok) {
          e.preventDefault();
          showFormError(form, result.message);
          focusFirstEditableAmount(scope, result.state?.total);
          return;
        }
      }
      clearFormError(form);
    });

    form.addEventListener("input", (e) => {
      if (e.target?.matches?.("input.env-input, input[name='amount']")) clearFormError(form);
    });
    form.addEventListener("change", (e) => {
      if (e.target?.matches?.("[data-remainder-select], select[name$='account_id'], select.env-mode")) clearFormError(form);
    });
  }

  function wireDismissValidation(scopeEl) {
    const modal = scopeEl.closest(".modal");
    if (!modal || modal.__envDismissValidationBound) return;
    modal.__envDismissValidationBound = true;

    modal.addEventListener("click", (e) => {
      const trigger = e.target?.closest?.("[data-validate-env-dismiss]");
      if (!trigger || !modal.contains(trigger)) return;

      const scopes = Array.from(modal.querySelectorAll(".env-scope[data-total-selector]"));
      for (const scope of scopes) {
        const result = validateEnvelopeScope(scope);
        if (!result.ok) {
          e.preventDefault();
          e.stopImmediatePropagation();
          showFormError(modal, result.message);
          focusFirstEditableAmount(scope, result.state?.total);
          return;
        }
      }
      clearFormError(modal);
    }, true);

    modal.addEventListener("input", (e) => {
      if (e.target?.matches?.("input.env-input, input[name='amount'], input[type='checkbox']")) clearFormError(modal);
    });
    modal.addEventListener("change", (e) => {
      if (e.target?.matches?.("[data-remainder-select], select, input[type='checkbox']")) clearFormError(modal);
    });
  }

  function initScope(scopeEl) {
    if (!scopeEl || scopeEl.__envFilterInit) return;
    scopeEl.__envFilterInit = true;

    captureTemplates(scopeEl);
    wireFormValidation(scopeEl);
    wireDismissValidation(scopeEl);

    const sel = accountSelect(scopeEl);
    if (sel) sel.addEventListener("change", () => rebuild(scopeEl), { passive: true });

    const modal = scopeEl.closest(".modal");
    if (modal) {
      modal.addEventListener("show.bs.modal", () => rebuild(scopeEl));
      modal.addEventListener("shown.bs.modal", () => requestAnimationFrame(() => rebuild(scopeEl)));
    }
    setTimeout(() => rebuild(scopeEl), 0);
  }

  function initAll() {
    // Ensure balances JSON is present once at startup (soft check)
    getBalancesJSON();
    document.querySelectorAll(".env-scope").forEach(initScope);
  }

  READY(initAll);
  window.__applyEnvelopeFilters = initAll;
})();
