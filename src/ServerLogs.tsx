import React from "react";
import { Box, List, ListItem, Typography } from "@mui/material";
import { styled } from "@mui/system";
import { useLogs } from "./util";
import Loader from "./Loader";

const ScrollPanel = styled(Box)(() => ({
    maxHeight: "400px",
    overflowY: "auto",
}));

const ServerLogPanel: React.FC = () => {
    const { logs, fetchLogs, isLoading } = useLogs();
    React.useEffect(() => {
        fetchLogs();
        const ival = setInterval(fetchLogs, 60000);
        return () => {
            clearInterval(ival);
        };
    }, [fetchLogs]);

    return (
        <ScrollPanel sx={{ position: "relative" }}>
            <Loader open={isLoading} />
            <List>
                {logs.map((log, index) => (
                    <ListItem key={index}>
                        <Typography variant="body1">{log}</Typography>
                    </ListItem>
                ))}
            </List>
        </ScrollPanel>
    );
};

export default ServerLogPanel;
