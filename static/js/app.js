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
