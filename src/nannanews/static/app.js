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
})();
