document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  document.querySelectorAll("[data-pick-form]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      const row = form.closest("tr");
      const name = row?.querySelector("td:nth-child(2) strong")?.textContent || "this player";
      if (!window.confirm(`Draft ${name} with the current pick?`)) event.preventDefault();
    });
  });

  const correctionForm = document.querySelector("#correction-form");
  const correctionPick = document.querySelector("#correction-pick");
  correctionForm?.addEventListener("submit", (event) => {
    correctionForm.action = `/picks/${correctionPick.value}/correct`;
    if (!window.confirm("Replace the player at this saved pick?")) event.preventDefault();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "/" && !["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) {
      event.preventDefault();
      document.querySelector("#player-search")?.focus();
    }
  });
});

