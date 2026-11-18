"use strict";

function formatSize(bytes) {
  if (bytes < 1000) return bytes + " B";
  if (bytes < 1e6) return (bytes / 1000).toFixed(1) + " KB";
  if (bytes < 1e9) return (bytes / 1e6).toFixed(1) + " MB";
  return (bytes / 1e9).toFixed(2) + " GB";
}

for (const form of document.querySelectorAll(".save-file")) {
  const button = form.querySelector("button");
  const status = form.querySelector(".status");
  const progress = form.querySelector(".progress");
  const name = form.querySelector(".saved-name");
  const copy = form.querySelector(".copy");

  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(name.textContent);
      status.textContent = "Filename copied.";
    } catch {
      const range = document.createRange();
      range.selectNodeContents(name);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      status.textContent = "Filename selected. Use your browser’s Copy command.";
    }
  });

  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (button.disabled) return;
    button.disabled = true;
    button.textContent = "progress…";
    status.textContent = "In progress…";
    progress.textContent = "";
    try {
      const response = await fetch(form.action, {
        method: "POST",
        headers: { Accept: "application/x-ndjson" },
        body: new URLSearchParams(new FormData(form))
      });
      if (!response.ok) {
        throw new Error(response.status === 403 ? "Refresh the file list and try again." :
          "The server returned HTTP " + response.status + ".");
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffered = "";
      while (true) {
        const chunk = await reader.read();
        if (chunk.done) throw new Error("Confirmation lost. Check the save/ folder before retrying.");
        buffered += decoder.decode(chunk.value, { stream: true });
        const lines = buffered.split("\n");
        buffered = lines.pop();
        for (const line of lines) {
          const update = JSON.parse(line);
          if (update.state === "error") throw new Error(update.message);
          if (update.state === "progress") {
            progress.textContent = formatSize(update.received) +
              (update.total != null ? " of " + formatSize(Number(update.total)) : " received");
          }
          if (update.state === "saved") {
            name.textContent = update.filename;
            status.textContent = "Done in save/";
            progress.textContent = "";
            button.textContent = "done";
            copy.hidden = false;
            return;
          }
        }
      }
    } catch (error) {
      status.textContent = error instanceof TypeError ?
        "Connection lost. Check the save/ folder before retrying." : error.message;
      progress.textContent = "";
      button.textContent = "Retry";
      button.removeAttribute("aria-label");
      button.disabled = false;
    }
  });
}
