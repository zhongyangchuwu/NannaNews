(() => {
  'use strict';

  const token = document.querySelector('meta[name="csrf-token"]')?.content;
  const addCsrfToken = (form) => {
    if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== 'post') return;
    if (form.elements.namedItem('csrf_token') || !token) return;
    const input = document.createElement('input');
    input.type = 'hidden';
    input.name = 'csrf_token';
    input.value = token;
    form.append(input);
  };

  // Populate every rendered POST form before interaction, including legacy views.
  document.querySelectorAll('form').forEach(addCsrfToken);
  // Cover forms inserted later. Programmatic form.submit() callers must add their own token.
  document.addEventListener('submit', (event) => addCsrfToken(event.target), true);

  document.querySelectorAll('form[data-submit-once]').forEach((form) => {
    let submitted = false;
    const status = form.querySelector('[data-submit-status]');
    const idleText = status?.textContent;
    form.addEventListener('submit', (event) => {
      if (event.defaultPrevented) return;
      if (submitted) { event.preventDefault(); return; }
      submitted = true;
      form.setAttribute('aria-busy', 'true');
      if (status) status.textContent = form.dataset.submitMessage;
    });
    window.addEventListener('pageshow', (event) => {
      if (!event.persisted) return;
      submitted = false;
      form.removeAttribute('aria-busy');
      if (status) status.textContent = idleText;
    });
  });
})();
