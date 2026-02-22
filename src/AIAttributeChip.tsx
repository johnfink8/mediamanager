import React, { useMemo, useState } from "react";
import {
    Chip,
    Dialog,
    DialogTitle,
    DialogContent,
    DialogActions,
    Button,
    Box,
    Typography,
    Stack,
    Divider,
    Alert,
} from "@mui/material";
import { graphql, useMutation } from "react-relay";
import { AIAttributeChipRetryAiMutation } from "./__generated__/AIAttributeChipRetryAiMutation.graphql";

interface AIAttributeChipProps {
    details: Record<string, unknown> | null | undefined;
    itemId?: string;
}

function formatJson(value: unknown): string {
    try {
        return JSON.stringify(value, null, 2);
    } catch {
        return String(value);
    }
}

const RetryAIMutation = graphql`
    mutation AIAttributeChipRetryAiMutation($input: RetryAiInput!) {
        retryAi(data: $input) {
            id
            type
            uid
            title
            added
            checkedTitle
            posterUrl
            attributes {
                key
                values
                details
            }
        }
    }
`;

const AIAttributeChip: React.FC<AIAttributeChipProps> = ({
    details,
    itemId,
}) => {
    const [open, setOpen] = useState(false);
    const [commitRetry, isRetrying] =
        useMutation<AIAttributeChipRetryAiMutation>(RetryAIMutation);

    if (!details || typeof details !== "object") {
        return null;
    }

    const ai = details as Record<string, unknown>;
    const recommended = useMemo(() => {
        const val = ai["value"];
        if (typeof val === "boolean") return val;
        if (typeof val === "string") return val.toLowerCase() === "true";
        return undefined;
    }, [ai]);

    const score = ai["score"] ?? ai["ai_score"];
    const reason = ai["reason"] ?? ai["ai_reason"];
    const similar = (ai["similar_refs"] ?? ai["ai_similar_refs"]) as unknown;
    const failure = ai["failure"] as Record<string, unknown> | null | undefined;
    const failureMessage = useMemo(() => {
        if (!failure || typeof failure !== "object") return null;
        const code =
            typeof failure["code"] === "string" ? ` (${failure["code"]})` : "";
        const message =
            typeof failure["message"] === "string" ? failure["message"] : null;
        const stage =
            typeof failure["stage"] === "string" ? failure["stage"] : null;
        const parts = [
            stage ? stage.charAt(0).toUpperCase() + stage.slice(1) : null,
            message,
        ].filter(Boolean);
        if (parts.length === 0 && !code) {
            return "AI request failed. Please retry.";
        }
        return `${parts.join(": ")}${code}`;
    }, [failure]);

    const hasSummary =
        recommended !== undefined ||
        typeof score === "number" ||
        typeof reason === "string";

    const chipLabel = useMemo(() => {
        if (failure) {
            return "AI: Failed";
        }
        if (recommended === true) {
            return typeof score === "number"
                ? `AI: Recommended (${(score as number).toFixed(2)})`
                : "AI: Recommended";
        }
        if (recommended === false) {
            return "AI: Not Recommended";
        }
        return "AI";
    }, [failure, recommended, score]);

    const handleRetry = () => {
        if (!itemId) return;
        commitRetry({
            variables: {
                input: {
                    id: itemId,
                },
            },
        });
    };

    return (
        <>
            <Chip
                label={chipLabel}
                color={
                    recommended === true
                        ? "success"
                        : recommended === false
                        ? "default"
                        : "primary"
                }
                sx={{ mr: 1, mb: 1, fontWeight: 600 }}
                onClick={() => setOpen(true)}
            />
            <Dialog
                open={open}
                onClose={() => setOpen(false)}
                maxWidth="md"
                fullWidth
            >
                <DialogTitle>AI Assessment</DialogTitle>
                <DialogContent dividers>
                    <Stack spacing={2}>
                        {!!failureMessage && (
                            <Alert
                                severity="error"
                                sx={{ alignItems: "center" }}
                            >
                                {failureMessage}
                            </Alert>
                        )}
                        {hasSummary && (
                            <Box>
                                <Typography
                                    variant="subtitle1"
                                    sx={{ fontWeight: 700, mb: 1 }}
                                >
                                    Summary
                                </Typography>
                                {recommended !== undefined && (
                                    <Typography variant="body1">
                                        Recommendation:{" "}
                                        {recommended
                                            ? "Recommended"
                                            : "Not recommended"}
                                    </Typography>
                                )}
                                {typeof score === "number" && (
                                    <Typography variant="body1">
                                        Score: {(score as number).toFixed(3)}
                                    </Typography>
                                )}
                                {typeof reason === "string" && (
                                    <Typography
                                        variant="body1"
                                        sx={{ whiteSpace: "pre-wrap", mt: 1 }}
                                    >
                                        {String(reason)}
                                    </Typography>
                                )}
                            </Box>
                        )}
                        {Array.isArray(similar) && similar.length > 0 && (
                            <Box>
                                <Typography
                                    variant="subtitle1"
                                    sx={{ fontWeight: 700, mb: 1 }}
                                >
                                    Similar References
                                </Typography>
                                <Stack spacing={0.5}>
                                    {(similar as unknown[]).map((ref, idx) => (
                                        <Typography
                                            key={idx}
                                            variant="body2"
                                            sx={{ wordBreak: "break-word" }}
                                        >
                                            {typeof ref === "string"
                                                ? ref
                                                : formatJson(ref)}
                                        </Typography>
                                    ))}
                                </Stack>
                            </Box>
                        )}
                        <Divider />
                        <Box>
                            <Typography
                                variant="subtitle1"
                                sx={{ fontWeight: 700, mb: 1 }}
                            >
                                Full Details
                            </Typography>
                            <Box
                                component="pre"
                                sx={{
                                    m: 0,
                                    whiteSpace: "pre-wrap",
                                    wordBreak: "break-word",
                                }}
                            >
                                {formatJson(details)}
                            </Box>
                        </Box>
                    </Stack>
                </DialogContent>
                <DialogActions>
                    {failure && itemId && (
                        <Button
                            onClick={handleRetry}
                            variant="contained"
                            disabled={isRetrying}
                        >
                            {isRetrying ? "Retrying..." : "Retry AI"}
                        </Button>
                    )}
                    <Button onClick={() => setOpen(false)}>Close</Button>
                </DialogActions>
            </Dialog>
        </>
    );
};

export default AIAttributeChip;
