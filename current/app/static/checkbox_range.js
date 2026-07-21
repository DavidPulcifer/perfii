(function (global) {
  'use strict';

  const SCOPE_SELECTOR = '[data-checkbox-range-scope]';
  const CHECKBOX_SELECTOR = 'input[type="checkbox"][data-range-checkbox]';

  function notifyCheckboxChange(checkbox) {
    checkbox.dispatchEvent(new Event('input', { bubbles: true }));
    checkbox.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function setCheckedAndNotify(checkbox, checked) {
    if (!checkbox || checkbox.disabled) return false;
    const desired = !!checked;
    if (checkbox.checked === desired) return false;
    checkbox.checked = desired;
    notifyCheckboxChange(checkbox);
    return true;
  }

  function scopedCheckboxes(scope) {
    if (!scope) return [];
    return Array.from(scope.querySelectorAll(CHECKBOX_SELECTOR)).filter((checkbox) => {
      return !checkbox.disabled && checkbox.closest(SCOPE_SELECTOR) === scope;
    });
  }

  function applyCheckboxRange(scope, anchor, target) {
    const boxes = scopedCheckboxes(scope);
    const anchorIndex = boxes.indexOf(anchor);
    const targetIndex = boxes.indexOf(target);
    if (anchorIndex < 0 || targetIndex < 0) return false;

    const start = Math.min(anchorIndex, targetIndex);
    const end = Math.max(anchorIndex, targetIndex);
    const checked = !!target.checked;

    boxes.slice(start, end + 1).forEach((checkbox) => {
      if (checkbox === target) return;
      setCheckedAndNotify(checkbox, checked);
    });
    return true;
  }

  function installShiftClickCheckboxRanges(root) {
    const eventRoot = root || document;
    eventRoot.addEventListener('click', (event) => {
      const target = event.target?.closest?.(CHECKBOX_SELECTOR);
      if (!target || target.disabled) return;

      const scope = target.closest(SCOPE_SELECTOR);
      if (!scope) return;

      const anchor = scope.__checkboxRangeAnchor;
      if (event.shiftKey && anchor) {
        applyCheckboxRange(scope, anchor, target);
      }
      scope.__checkboxRangeAnchor = target;
    });
  }

  global.FinanceCheckboxRanges = {
    install: installShiftClickCheckboxRanges,
    _test: {
      applyCheckboxRange,
      scopedCheckboxes,
      setCheckedAndNotify,
    },
  };

  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', () => installShiftClickCheckboxRanges(document));
    } else {
      installShiftClickCheckboxRanges(document);
    }
  }
})(window);
