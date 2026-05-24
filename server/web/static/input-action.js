// .input-action — merged Paste/Submit affordance.
//
// One button next to a text-ish input. When the field is empty, the
// button is a Paste-from-clipboard helper (type=button, clipboard
// icon). When the field has a value, it's a Submit (type=submit,
// arrow icon) that lets the surrounding <form> handle the
// submission (including HTMX hx-post).
//
// The initial markup uses type=submit / data-state=submit so a
// no-JS visitor (or a password manager autofilling) still gets a
// working button. This script downgrades the button to paste mode
// only after it has observed the field is empty.
//
// Silent on failure: a denied clipboard permission or an insecure
// context just leaves the button as-is (manual typing still works).

(function () {
  function evalState(input, btn) {
    var has = !!(input.value && input.value.trim());
    btn.dataset.state = has ? 'submit' : 'paste';
    btn.type = has ? 'submit' : 'button';
    btn.setAttribute(
      'aria-label',
      has
        ? (btn.dataset.submitLabel || 'Submit')
        : 'Paste from clipboard'
    );
  }

  function init(wrap) {
    var input = wrap.querySelector('input');
    var btn = wrap.querySelector('.input-action-btn');
    if (!input || !btn) return;
    // Remember the markup-supplied aria-label so we can restore it
    // when flipping back to submit mode after a clear+paste cycle.
    btn.dataset.submitLabel = btn.dataset.submitLabel ||
      btn.getAttribute('aria-label') || 'Submit';
    evalState(input, btn);
    input.addEventListener('input', function () { evalState(input, btn); });
    btn.addEventListener('click', function (e) {
      if (btn.dataset.state !== 'paste') {
        // Submit mode: let the click submit the parent form natively
        // (HTMX hx-post wires off the same submit event).
        return;
      }
      e.preventDefault();
      if (!navigator.clipboard || !navigator.clipboard.readText) return;
      navigator.clipboard.readText().then(function (text) {
        if (!text) return;
        input.value = text.trim();
        // Dispatch input so anything else listening (state eval, htmx
        // input-changed triggers) sees the value.
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.focus();
      }, function () { /* permission denied / insecure context */ });
    });
  }

  function boot() {
    document.querySelectorAll('.input-action').forEach(init);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
