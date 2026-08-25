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

  document.querySelectorAll("[data-ai-model-form]").forEach((form) => {
    const select = form.querySelector("select[name='model']");
    const status = form.querySelector("[data-ai-model-status]");
    let savedModel = select?.value;
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!select) return;
      const requestedModel = select.value;
      if (status) status.textContent = "Saving…";
      try {
        const response = await fetch(form.action, {
          method: "POST",
          headers: { Accept: "application/json" },
          body: new FormData(form),
        });
        if (!response.ok) throw new Error("model selection failed");
        savedModel = requestedModel;
        document.querySelectorAll("[data-ai-retry-model]").forEach((input) => {
          input.value = requestedModel;
        });
        if (status) status.textContent = `${requestedModel} selected`;
      } catch (_error) {
        select.value = savedModel;
        if (status) status.textContent = "Could not save model";
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
