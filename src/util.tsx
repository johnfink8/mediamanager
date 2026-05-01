import React, { useState, useCallback } from "react";
import { useSnackbar } from "material-ui-snackbar-provider";
import { LiveTv, Movie, FactCheck } from "@mui/icons-material";
import HistoryIcon from "@mui/icons-material/History";
import SettingsIcon from "@mui/icons-material/Settings";
import { itemType, MenuItemType } from "./types";
import ItemList from "./ItemList";
import HistoricalItemList from "./HistoricalItemList";
import CheckFeedbackPanel from "./CheckFeedbackPanel";
import AdminPanel from "./AdminPanel";

export const tvHref = (uid: string) => {
    return `http://thetvdb.com/?tab=series&id=${uid}`;
};

export const movieHref = (uid: string) => {
    return `http://www.imdb.com/title/${uid}`;
};

export const itemLink = (item: itemType): string | undefined => {
    if (item.type === "mv") {
        return movieHref(item.uid);
    }
    if (item.type === "tv") {
        return tvHref(item.uid);
    }
    return undefined;
};

export const useLogs = () => {
    const [logs, setLogs] = useState<string[]>([]);
    const [isLoading, setLoading] = useState<boolean>(true);
    const snackbar = useSnackbar();

    const fetchLogs = useCallback(() => {
        fetch("/logs/").then((response) => {
            if (response.status !== 200) {
                response
                    .text()
                    .then((t) => snackbar.showMessage(t.slice(0, 200)));
                setLoading(false);
                return;
            }
            response.json().then((data) => {
                setLogs(data);
                setLoading(false);
            });
        });
    }, [setLogs]);

    return {
        logs,
        isLoading,
        fetchLogs,
    };
};

export const menuItems: MenuItemType[] = [
    {
        name: "Movies",
        icon: <Movie />,
        component: ItemList,
        tabIndex: 0,
        typeName: "mv",
    },
    {
        name: "TV",
        icon: <LiveTv />,
        component: ItemList,
        tabIndex: 1,
        typeName: "tv",
    },
    {
        name: "Item History",
        icon: <HistoryIcon />,
        component: HistoricalItemList,
        tabIndex: 2,
        typeName: "mv",
    },
    {
        name: "Check Feedback",
        icon: <FactCheck />,
        component: CheckFeedbackPanel,
        tabIndex: undefined,
        typeName: undefined,
    },
    {
        name: "Admin",
        icon: <SettingsIcon />,
        component: AdminPanel,
        tabIndex: undefined,
        typeName: undefined,
    },
];
