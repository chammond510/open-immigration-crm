const menuButton = document.querySelector("[data-menu-toggle]");
const sidebar = document.querySelector("#sidebar");

if (menuButton && sidebar) {
  menuButton.addEventListener("click", () => {
    const open = sidebar.classList.toggle("is-open");
    menuButton.setAttribute("aria-expanded", String(open));
  });
}
document.querySelectorAll("form[data-confirm]").forEach((form) => {
  form.addEventListener("submit", (event) => {
    if (!window.confirm(form.dataset.confirm)) {
      event.preventDefault();
    }
  });
});

const timerElapsed = document.querySelector("[data-timer-started]");

if (timerElapsed) {
  const startedAt = new Date(timerElapsed.dataset.timerStarted);
  const renderElapsed = () => {
    const totalSeconds = Math.max(0, Math.floor((Date.now() - startedAt.getTime()) / 1000));
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = String(Math.floor((totalSeconds % 3600) / 60)).padStart(2, "0");
    const seconds = String(totalSeconds % 60).padStart(2, "0");
    timerElapsed.textContent = `${hours}:${minutes}:${seconds}`;
  };
  renderElapsed();
  setInterval(renderElapsed, 1000);
}

const copyButton = document.querySelector("[data-copy-button]");
const copySource = document.querySelector("[data-copy-source]");

if (copyButton && copySource) {
  copyButton.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(copySource.value);
      copyButton.textContent = "Copied";
    } catch {
      copySource.select();
      copyButton.textContent = "Link selected — copy it";
    }
  });
}

document.querySelectorAll("[data-copy-for]").forEach((button) => {
  button.addEventListener("click", async () => {
    const source = document.getElementById(button.dataset.copyFor);
    try {
      await navigator.clipboard.writeText(source.value);
      button.textContent = "Copied";
    } catch {
      source.select();
      button.textContent = "Selected — copy it";
    }
  });
});

document.querySelectorAll("[data-print]").forEach((button) => {
  button.addEventListener("click", () => window.print());
});

// While AI work runs in the background, check on it and reload when it is done.
const pending = document.querySelector("[data-poll-url]");

if (pending) {
  const startedAt = Date.now();
  const check = async () => {
    try {
      const response = await fetch(pending.dataset.pollUrl, {
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      });
      if (response.ok && (await response.json()).done) {
        window.location.reload();
        return;
      }
    } catch {
      // Try again on the next tick.
    }
    if (Date.now() - startedAt < 10 * 60 * 1000) {
      setTimeout(check, 3000);
    }
  };
  setTimeout(check, 3000);
}
