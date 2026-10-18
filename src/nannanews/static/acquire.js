(() => {
  'use strict';
  const form = document.getElementById('acquire-form');
  if (!form) return;
  const button = document.getElementById('acquire-submit');
  const progress = document.getElementById('acquire-progress');
  const images = document.getElementById('public-images');
  const url = document.getElementById('source-url');
  const text = document.getElementById('public-text');
  form.addEventListener('submit', (event) => {
    const files = Array.from(images.files || []);
    let message = '';
    if (!url.value.trim() && !text.value.trim() && !files.length) {
      message = '请输入公开 URL，或提供公开正文/图片。';
    } else if (files.length > 4 || files.some((file) => file.size > 3 * 1024 * 1024)) {
      message = '最多上传 4 张图片，每张不超过 3 MiB。';
    }
    if (message) {
      event.preventDefault();
      progress.textContent = message;
      progress.className = 'notice notice-error';
      return;
    }
    // Normal form submission preserves values/errors and does not create a second API path.
    button.disabled = true;
    button.textContent = '采集中…';
    progress.className = 'notice';
    progress.textContent = '正在读取公开证据并调用模型。随后将验证引用和日期，再原子写入待审核草稿；请勿重复提交。';
    form.setAttribute('aria-busy', 'true');
  });
  window.addEventListener('pageshow', () => {
    if (form.getAttribute('aria-busy') === 'true') {
      form.removeAttribute('aria-busy');
      button.disabled = false;
      button.textContent = '提取为待审核草稿';
      progress.textContent = '';
    }
  });
})();
