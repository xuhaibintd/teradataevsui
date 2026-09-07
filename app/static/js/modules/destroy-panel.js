(function (global) {
  "use strict";

  const app = global.EVSUIApp;

  function bindDestroyConfirmModal(scope = document) {
    const panels = scope.querySelectorAll("[data-vs-destroy-panel]");
    panels.forEach((panel) => {
      const triggerButton = panel.querySelector("[data-destroy-btn]");
      const destroyForm = triggerButton ? triggerButton.closest("form") : null;
      const modal = panel.querySelector("[data-destroy-confirm]");
      const modalName = panel.querySelector("[data-confirm-vs-name]");
      const nameInput = destroyForm ? destroyForm.querySelector("[data-destroy-vs-input]") : null;
      const cancelButtons = panel.querySelectorAll("[data-confirm-cancel]");
      const okButton = panel.querySelector("[data-confirm-ok]");
      if (!(triggerButton instanceof HTMLButtonElement) || !(destroyForm instanceof HTMLFormElement) || !(modal instanceof HTMLElement)) {
        return;
      }
      if (triggerButton.dataset.confirmBound === "1") {
        return;
      }
      triggerButton.dataset.confirmBound = "1";

      const closeModal = () => {
        modal.hidden = true;
        document.body.classList.remove("confirm-open");
      };

      const currentVsName = () => (nameInput instanceof HTMLInputElement ? nameInput.value : "").trim();

      const openModal = () => {
        const name = currentVsName();
        if (modalName) {
          modalName.textContent = name;
        }
        modal.hidden = false;
        document.body.classList.add("confirm-open");
        if (okButton instanceof HTMLButtonElement) {
          okButton.focus();
        }
      };

      triggerButton.addEventListener("click", (event) => {
        if (triggerButton.dataset.confirmArmed === "1") {
          delete triggerButton.dataset.confirmArmed;
          return;
        }
        event.preventDefault();
        openModal();
      });

      cancelButtons.forEach((button) => {
        button.addEventListener("click", closeModal);
      });

      if (okButton instanceof HTMLButtonElement) {
        okButton.addEventListener("click", () => {
          const name = currentVsName();
          closeModal();
          if (!name) {
            app.setTopMessage("Select a vector store before deleting.", "warn");
            return;
          }
          app.setTopMessage(`Deleting '${name}'...`, "info");
          if (typeof destroyForm.requestSubmit === "function") {
            destroyForm.requestSubmit(triggerButton);
            return;
          }
          triggerButton.dataset.confirmArmed = "1";
          setTimeout(() => triggerButton.click(), 0);
        });
      }

      destroyForm.addEventListener("htmx:afterRequest", (event) => {
        const source = event.detail && event.detail.elt;
        if (source !== destroyForm) {
          return;
        }
        if (event.detail && event.detail.successful) {
          return;
        }
        const xhr = event.detail && event.detail.xhr;
        const status = xhr && typeof xhr.status === "number" ? xhr.status : 0;
        const suffix = status ? ` (HTTP ${status})` : "";
        app.setTopMessage(`Delete request failed for '${currentVsName()}'.${suffix}`, "err");
      });

      destroyForm.addEventListener("htmx:sendError", (event) => {
        const source = event.detail && event.detail.elt;
        if (source !== destroyForm) {
          return;
        }
        app.setTopMessage(`Delete request could not be sent for '${currentVsName()}'.`, "err");
      });

      destroyForm.addEventListener("htmx:timeout", (event) => {
        const source = event.detail && event.detail.elt;
        if (source !== destroyForm) {
          return;
        }
        app.setTopMessage(`Delete request timed out for '${currentVsName()}'.`, "err");
      });

      panel.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && !modal.hidden) {
          event.preventDefault();
          closeModal();
        }
      });
    });
  }

  app.bindDestroyConfirmModal = bindDestroyConfirmModal;

  app.registerBinder(bindDestroyConfirmModal);
})(window);
