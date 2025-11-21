import React from "react";
import {
    Box,
    Button,
    Card,
    CardContent,
    Chip,
    Divider,
    Stack,
    Typography,
} from "@mui/material";
import {
    graphql,
    PreloadedQuery,
    usePreloadedQuery,
    useQueryLoader,
} from "react-relay/hooks";

import Loader from "./Loader";
import { MenuItemType } from "./types";
import { CheckFeedbackPanelQuery } from "./__generated__/CheckFeedbackPanelQuery.graphql";

const CheckFeedbackQueryNode = graphql`
    query CheckFeedbackPanelQuery {
        checkRuns {
            movies {
                kind
                timestamp
                durationMs
                success
                message
                error
                checkedCount
                checkedItems {
                    title
                    uid
                    ignored
                    note
                }
            }
            shows {
                kind
                timestamp
                durationMs
                success
                message
                error
                checkedCount
                checkedItems {
                    title
                    uid
                    ignored
                    note
                }
            }
        }
    }
`;

const formatDate = (timestamp?: string | null): string => {
    if (!timestamp) {
        return "Unknown time";
    }
    const date = new Date(timestamp);
    return isNaN(date.getTime()) ? timestamp : date.toLocaleString();
};

type RunType = CheckFeedbackPanelQuery["response"]["checkRuns"]["movies"][number];

const RunCard: React.FC<{ run: RunType }> = ({ run }) => (
    <Card variant="outlined">
        <CardContent>
            <Box display="flex" justifyContent="space-between" alignItems="center">
                <Typography variant="h6">{formatDate(run.timestamp)}</Typography>
                <Chip
                    label={run.success ? "Success" : "Error"}
                    color={run.success ? "success" : "error"}
                    size="small"
                />
            </Box>
            <Typography variant="body1" sx={{ mt: 1 }}>
                {run.message}
            </Typography>
            {run.error && (
                <Typography variant="body2" color="error" sx={{ mt: 0.5 }}>
                    {run.error}
                </Typography>
            )}
            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
                Duration: {run.durationMs} ms · Items processed: {run.checkedCount}
            </Typography>
            <Divider sx={{ my: 1 }} />
            <Stack spacing={1}>
                {run.checkedItems.length === 0 && (
                    <Typography variant="body2" color="text.secondary">
                        No new items were recorded in this run.
                    </Typography>
                )}
                {run.checkedItems.map((item) => (
                    <Box key={`${run.timestamp}-${item.uid}`}>
                        <Typography variant="subtitle2">{item.title}</Typography>
                        <Typography variant="body2" color="text.secondary">
                            {item.uid} · {item.note || "Processed"}
                            {item.ignored ? " · Ignored" : ""}
                        </Typography>
                    </Box>
                ))}
            </Stack>
        </CardContent>
    </Card>
);

const RunList: React.FC<{ title: string; runs: readonly RunType[] }> = ({ title, runs }) => (
    <Box sx={{ mt: 2 }}>
        <Typography variant="h5" gutterBottom>
            {title}
        </Typography>
        <Stack spacing={2}>
            {runs.length === 0 && (
                <Typography variant="body2" color="text.secondary">
                    No history recorded yet.
                </Typography>
            )}
            {runs.map((run) => (
                <RunCard key={`${run.kind}-${run.timestamp}`} run={run} />
            ))}
        </Stack>
    </Box>
);

const CheckFeedbackContent: React.FC<{
    queryRef: PreloadedQuery<CheckFeedbackPanelQuery>;
    onRefresh: () => void;
}> = ({ queryRef, onRefresh }) => {
    const data = usePreloadedQuery(CheckFeedbackQueryNode, queryRef);

    return (
        <Box>
            <Box display="flex" justifyContent="flex-end" sx={{ mb: 2 }}>
                <Button variant="outlined" onClick={onRefresh}>
                    Refresh
                </Button>
            </Box>
            <RunList title="Movies" runs={data.checkRuns.movies} />
            <RunList title="TV" runs={data.checkRuns.shows} />
        </Box>
    );
};

const CheckFeedbackPanel: React.FC<{ menuItem: MenuItemType }> = () => {
    const [queryRef, loadQuery, disposeQuery] = useQueryLoader<CheckFeedbackPanelQuery>(
        CheckFeedbackQueryNode,
    );

    React.useEffect(() => {
        loadQuery({});
        return () => disposeQuery();
    }, [loadQuery, disposeQuery]);

    if (!queryRef) {
        return <Loader open />;
    }

    return (
        <React.Suspense fallback={<Loader open />}>
            <CheckFeedbackContent queryRef={queryRef} onRefresh={() => loadQuery({}, { fetchPolicy: "network-only" })} />
        </React.Suspense>
    );
};

export default CheckFeedbackPanel;
