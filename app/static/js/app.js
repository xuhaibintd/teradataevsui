import "./step-gate.js?v=20260905-2";
import "./modules/core.js?v=20260905-2";
import "./modules/create-uploads.js?v=20260905-2";
import "./modules/create-params.js?v=20260905-2";
import "./modules/destroy-panel.js?v=20260905-3";
import "./modules/management-panel.js?v=20260905-2";
import "./modules/htmx-progress.js?v=20260905-2";
import "./modules/navigation.js?v=20260409-3";
import "./modules/chat-retrieval.js?v=20260331-1";
import "./modules/json-inspector.js?v=20260905-2";

(function (global) {
  "use strict";

  const app = global.EVSUIApp || {};

  document.addEventListener("DOMContentLoaded", () => {
    if (typeof global.createStepGate === "function") {
      app.stepGate = global.createStepGate();
      app.stepGate.initialize();
    }

    if (typeof app.registerNavigation === "function") {
      app.registerNavigation();
    }
    if (typeof app.registerHtmxProgressButtons === "function") {
      app.registerHtmxProgressButtons();
    }
    if (typeof app.registerHtmxAfterSwap === "function") {
      app.registerHtmxAfterSwap();
    }
    if (typeof app.bindAll === "function") {
      app.bindAll(document);
    }
  });
})(window);
