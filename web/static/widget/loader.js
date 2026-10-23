// The IngenMind chat widget (R50, app/channels/widget.py): a button in the
// corner of the embedding page that opens IngenMind's own chat page in a
// frame. Add with <script src="https://<ingenmind>/widget.js" data-widget="<id>" async>.
(() => {
  const script = document.currentScript;
  const id = script && script.dataset.widget;
  if (!id) return;
  const origin = new URL(script.src).origin;
  const root = document.createElement("div");
  root.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:2147483000;font:14px system-ui,sans-serif";
  const frame = document.createElement("iframe");
  frame.src = `${origin}/widget/${encodeURIComponent(id)}`;
  frame.title = script.dataset.title || "Ask us";
  frame.style.cssText = "display:none;width:min(380px,calc(100vw - 32px));height:min(560px,calc(100vh - 96px));"
    + "border:1px solid #d8dfde;border-radius:12px;box-shadow:0 8px 30px rgba(0,0,0,.18);background:#fff;margin-bottom:10px";
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = script.dataset.label || "Ask a question";
  button.setAttribute("aria-expanded", "false");
  button.style.cssText = "display:block;margin-left:auto;padding:10px 16px;border:0;border-radius:999px;"
    + "background:#1e3a8a;color:#fff;font:inherit;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.2)";
  button.addEventListener("click", () => {
    const open = frame.style.display === "none";
    frame.style.display = open ? "block" : "none";
    button.setAttribute("aria-expanded", String(open));
  });
  root.append(frame, button);
  document.body.append(root);
})();
