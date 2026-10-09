// Entry: mountFlowCanvas(element, {initialFlowId, session}) renders the flow
// builder and returns {unmount}. It loads, saves and runs flows through
// /api/flows itself; session() gives the browser session's {id, userId}.
import { createRoot } from "react-dom/client";
import { ReactFlowProvider } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import "./flows.css";
import { App } from "./App.jsx";

export function mountFlowCanvas(element, props) {
  const root = createRoot(element);
  root.render(
    <ReactFlowProvider>
      <App {...props} />
    </ReactFlowProvider>,
  );
  return { unmount: () => root.unmount() };
}
