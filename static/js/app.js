const menuButton = document.querySelector("[data-menu-toggle]");
const sidebar = document.querySelector("#sidebar");
const backdrop = document.querySelector("[data-menu-close]");

if (menuButton && sidebar) {
  const setOpen = (open) => {
    sidebar.classList.toggle("is-open", open);
    menuButton.setAttribute("aria-expanded", String(open));
    if (backdrop) backdrop.hidden = !open;
  };
  menuButton.addEventListener("click", () => setOpen(!sidebar.classList.contains("is-open")));
  if (backdrop) backdrop.addEventListener("click", () => setOpen(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && sidebar.classList.contains("is-open")) {
      setOpen(false);
      menuButton.focus();
    }
  });
}

// The account menu closes when you click elsewhere or press Escape.
const accountMenu = document.querySelector("[data-account-menu]");

if (accountMenu) {
  document.addEventListener("click", (event) => {
    if (accountMenu.open && !accountMenu.contains(event.target)) accountMenu.open = false;
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && accountMenu.open) {
      accountMenu.open = false;
      accountMenu.querySelector("summary").focus();
    }
  });
}

// Press / to search, unless you are already typing somewhere.
const searchInput = document.querySelector("[data-search-input]");

if (searchInput) {
  document.addEventListener("keydown", (event) => {
    const typing = event.target.closest("input, textarea, select, [contenteditable]");
    if (event.key === "/" && !typing && !event.metaKey && !event.ctrlKey && !event.altKey) {
      event.preventDefault();
      searchInput.focus();
      searchInput.select();
    }
  });
}
document.querySelectorAll("form[data-confirm]").forEach((form) => {
  form.addEventListener("submit", (event) => {
    if (!window.confirm(form.dataset.confirm)) {
      event.preventDefault();
    }
  });
});

// A second click while a form is sending would send it twice, so the button waits.
document.addEventListener("submit", (event) => {
  const form = event.target;
  if (event.defaultPrevented || form.method.toLowerCase() !== "post") return;
  const button = event.submitter;
  if (!button) return;
  // Disable after the browser has read the button's name and value.
  setTimeout(() => {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
  }, 0);
  setTimeout(() => {
    button.disabled = false;
    button.removeAttribute("aria-busy");
  }, 8000);
});

// Remember whether the person has typed into a form, so background work never
// reloads the page over an unsent draft.
let formsEdited = false;
document.addEventListener("input", (event) => {
  if (event.target.closest("form:not([role=search]):not(.filter-bar)")) formsEdited = true;
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

// Copy buttons say what happened, then go back to their label.
const copyText = async (button, source, fallback) => {
  const label = button.dataset.label || button.textContent;
  button.dataset.label = label;
  try {
    await navigator.clipboard.writeText(source.value);
    button.textContent = "Copied";
  } catch {
    source.select();
    button.textContent = fallback;
  }
  button.setAttribute("aria-live", "polite");
  clearTimeout(button.resetTimer);
  button.resetTimer = setTimeout(() => {
    button.textContent = label;
  }, 2500);
};

const copyButton = document.querySelector("[data-copy-button]");
const copySource = document.querySelector("[data-copy-source]");

if (copyButton && copySource) {
  copyButton.addEventListener("click", () => copyText(copyButton, copySource, "Link selected — copy it"));
}

document.querySelectorAll("[data-copy-for]").forEach((button) => {
  button.addEventListener("click", () => {
    copyText(button, document.getElementById(button.dataset.copyFor), "Selected — copy it");
  });
});

document.querySelectorAll("[data-print]").forEach((button) => {
  button.addEventListener("click", () => window.print());
});

// While AI work runs in the background, check on it and reload when it is done.
const pending = document.querySelector("[data-poll-url]");

if (pending) {
  const note = pending.matches("p") ? pending : pending.querySelector("p") || pending;
  const startedAt = Date.now();
  const check = async () => {
    try {
      const response = await fetch(pending.dataset.pollUrl, {
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      });
      if (response.ok && (await response.json()).done) {
        if (!formsEdited) {
          window.location.reload();
          return;
        }
        note.textContent = "Done. Reload the page to see it once you have saved your changes.";
        return;
      }
    } catch {
      // Try again on the next tick.
    }
    if (Date.now() - startedAt < 10 * 60 * 1000) {
      setTimeout(check, 3000);
    } else {
      note.textContent = "Still working. Reload the page later to check.";
    }
  };
  setTimeout(check, 3000);
}
