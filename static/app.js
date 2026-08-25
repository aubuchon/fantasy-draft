document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  document.querySelectorAll("[data-instant-pick-form]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (form.dataset.submitting === "true") {
        event.preventDefault();
        return;
      }
      form.dataset.submitting = "true";
      const button = form.querySelector("button[type='submit']");
      if (button) {
        button.disabled = true;
        button.textContent = "Saving…";
      }
    });
  });

  document.querySelectorAll("[data-ai-retry-form]").forEach((form) => {
    form.addEventListener("submit", () => {
      const button = form.querySelector("button[type='submit']");
      if (button) {
        button.disabled = true;
        button.textContent = "Asking AI…";
      }
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
