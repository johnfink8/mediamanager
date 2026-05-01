import React from "react";
import {
    Navigate,
    Route,
    Routes,
    useNavigate,
    useLocation,
} from "react-router-dom";
import { menuItems } from "./util";

const slugToName: Record<string, string> = {
    movies: "Movies",
    tv: "TV",
    history: "Item History",
    feedback: "Check Feedback",
    admin: "Admin",
};

const nameToSlug: Record<string, string> = Object.fromEntries(
    Object.entries(slugToName).map(([k, v]) => [v, k])
);

const FilmIcon = () => (
    <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.5}
        strokeLinecap="round"
        strokeLinejoin="round"
    >
        <rect x="3" y="3" width="18" height="18" rx="2" />
        <path d="M7 3v18M17 3v18M3 8h4M3 16h4M17 8h4M17 16h4M3 12h18" />
    </svg>
);

const TVIcon = () => (
    <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.5}
        strokeLinecap="round"
        strokeLinejoin="round"
    >
        <rect x="3" y="5" width="18" height="13" rx="2" />
        <path d="M8 21h8M12 18v3" />
    </svg>
);

const HistoryIcon = () => (
    <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.5}
        strokeLinecap="round"
        strokeLinejoin="round"
    >
        <path d="M3 12a9 9 0 1 0 3-6.7L3 8" />
        <path d="M3 3v5h5" />
        <path d="M12 7v5l3 2" />
    </svg>
);

const ToolIcon = () => (
    <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.5}
        strokeLinecap="round"
        strokeLinejoin="round"
    >
        <circle cx="12" cy="12" r="3" />
        <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 0 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 0 1 0-4h.1A1.7 1.7 0 0 0 4.6 9a1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 0 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 0 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" />
    </svg>
);

const navIconMap: Record<string, React.ReactElement> = {
    Movies: <FilmIcon />,
    TV: <TVIcon />,
    "Item History": <HistoryIcon />,
    "Check Feedback": <ToolIcon />,
    Admin: <ToolIcon />,
};

const queueItems = ["Movies", "TV"];
const archiveItems = ["Item History"];
const toolItems = ["Check Feedback"];
const systemItems = ["Admin"];

export default function AppShell() {
    const navigate = useNavigate();
    const { pathname } = useLocation();

    const slug = pathname.replace(/^\//, "") || "movies";
    const selectedComponent = slugToName[slug] ?? "Movies";

    return (
        <div className="app">
            <aside className="sidebar">
                <div className="brand">
                    <div className="brand-mark">A</div>
                    <div className="brand-name">
                        auto<em>·</em>dl
                    </div>
                </div>

                <div className="nav-label">Review queue</div>
                {menuItems
                    .filter((e) => queueItems.includes(e.name))
                    .map((entry) => (
                        <button
                            key={entry.name}
                            className={`nav-item${
                                selectedComponent === entry.name
                                    ? " active"
                                    : ""
                            }`}
                            onClick={() =>
                                navigate(`/${nameToSlug[entry.name]}`)
                            }
                        >
                            <span className="nav-icon">
                                {navIconMap[entry.name] ?? null}
                            </span>
                            <span>{entry.name}</span>
                        </button>
                    ))}

                <div className="nav-label">Archive</div>
                {menuItems
                    .filter((e) => archiveItems.includes(e.name))
                    .map((entry) => (
                        <button
                            key={entry.name}
                            className={`nav-item${
                                selectedComponent === entry.name
                                    ? " active"
                                    : ""
                            }`}
                            onClick={() =>
                                navigate(`/${nameToSlug[entry.name]}`)
                            }
                        >
                            <span className="nav-icon">
                                {navIconMap[entry.name] ?? null}
                            </span>
                            <span>{entry.name}</span>
                        </button>
                    ))}

                <div className="nav-label">Tools</div>
                {menuItems
                    .filter((e) => toolItems.includes(e.name))
                    .map((entry) => (
                        <button
                            key={entry.name}
                            className={`nav-item${
                                selectedComponent === entry.name
                                    ? " active"
                                    : ""
                            }`}
                            onClick={() =>
                                navigate(`/${nameToSlug[entry.name]}`)
                            }
                        >
                            <span className="nav-icon">
                                {navIconMap[entry.name] ?? null}
                            </span>
                            <span>{entry.name}</span>
                        </button>
                    ))}

                <div className="nav-label">System</div>
                {menuItems
                    .filter((e) => systemItems.includes(e.name))
                    .map((entry) => (
                        <button
                            key={entry.name}
                            className={`nav-item${
                                selectedComponent === entry.name
                                    ? " active"
                                    : ""
                            }`}
                            onClick={() =>
                                navigate(`/${nameToSlug[entry.name]}`)
                            }
                        >
                            <span className="nav-icon">
                                {navIconMap[entry.name] ?? null}
                            </span>
                            <span>{entry.name}</span>
                        </button>
                    ))}

                <div className="sidebar-footer">
                    <span className="status-dot" />
                    <div>
                        <div style={{ color: "var(--fg-dim)", fontSize: 11.5 }}>
                            AutoDownloader
                        </div>
                        <div
                            style={{
                                fontFamily: "JetBrains Mono, monospace",
                                fontSize: 11,
                                color: "var(--fg-mute)",
                            }}
                        >
                            connected
                        </div>
                    </div>
                </div>
            </aside>

            <main className="main">
                <div className="topbar">
                    <div className="crumbs">
                        <span>AutoDownloader</span>
                        <span>/</span>
                        <strong>{selectedComponent}</strong>
                    </div>
                </div>

                <Routes>
                    <Route
                        path="/"
                        element={<Navigate to="/movies" replace />}
                    />
                    {menuItems.map((entry) => (
                        <Route
                            key={entry.name}
                            path={`/${nameToSlug[entry.name]}`}
                            element={<entry.component menuItem={entry} />}
                        />
                    ))}
                </Routes>
            </main>
        </div>
    );
}
