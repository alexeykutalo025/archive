// Keep the transfer in the browser's download manager, even for large folders.
document.querySelectorAll('.folders a[download]').forEach(link => {
  const indicator = link.nextElementSibling;
  const bar = indicator.querySelector('progress');
  const percent = indicator.lastElementChild;
  let active = false;

  link.addEventListener('click', event => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    if (active) {
      event.preventDefault();
      return;
    }

    const bytes = crypto.getRandomValues(new Uint8Array(16));
    const token = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
    const original = link.href;
    const target = new URL(original);
    target.searchParams.set('progress', token);
    link.href = target.href;
    setTimeout(() => { link.href = original; }, 0);
    active = true;
    indicator.hidden = false;
    bar.value = 0;
    percent.textContent = '0%';
    let lastSeen = Date.now();

    function reset() {
      active = false;
      indicator.hidden = true;
    }

    async function poll() {
      try {
        const response = await fetch('/download-progress?progress=' + token, {cache: 'no-store'});
        if (response.ok) {
          const value = await response.json();
          if (value === -1) return reset();
          if (Number.isInteger(value)) {
            bar.value = value;
            percent.textContent = value + '%';
            lastSeen = Date.now();
            if (value === 100) return setTimeout(reset, 1100);
          }
        }
      } catch {
        // A brief status request failure should not interrupt the download.
      }
      if (Date.now() - lastSeen > 30000) return reset();
      setTimeout(poll, 350);
    }
    setTimeout(poll, 120);
  });
});
