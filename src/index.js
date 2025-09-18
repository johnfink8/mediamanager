import { createRoot } from "react-dom/client";
import App from "./App";
import "./app.less";

const root = createRoot(document.getElementById("app"));
root.render(App());
