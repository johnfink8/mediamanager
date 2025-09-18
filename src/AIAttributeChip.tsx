import React, { useMemo, useState } from "react";
import { Chip, Dialog, DialogTitle, DialogContent, DialogActions, Button, Box, Typography, Stack, Divider } from "@mui/material";

interface AIAttributeChipProps {
    details: Record<string, unknown> | null | undefined;
}

function formatJson(value: unknown): string {
    try {
        return JSON.stringify(value, null, 2);
    } catch {
        return String(value);
    }
}

const AIAttributeChip: React.FC<AIAttributeChipProps> = ({ details }) => {
    const [open, setOpen] = useState(false);

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
    const hasSummary = recommended !== undefined || typeof score === "number" || typeof reason === "string";

    const chipLabel = useMemo(() => {
        if (recommended === true) {
            return typeof score === "number" ? `AI: Recommended (${(score as number).toFixed(2)})` : "AI: Recommended";
        }
        if (recommended === false) {
            return "AI: Not Recommended";
        }
        return "AI";
    }, [recommended, score]);

    return (
        <>
            <Chip
                label={chipLabel}
                color={recommended === true ? "success" : recommended === false ? "default" : "primary"}
                sx={{ mr: 1, mb: 1, fontWeight: 600 }}
                onClick={() => setOpen(true)}
            />
            <Dialog open={open} onClose={() => setOpen(false)} maxWidth="md" fullWidth>
                <DialogTitle>AI Assessment</DialogTitle>
                <DialogContent dividers>
                    <Stack spacing={2}>
                        {hasSummary && (
                            <Box>
                                <Typography variant="subtitle1" sx={{ fontWeight: 700, mb: 1 }}>
                                    Summary
                                </Typography>
                                {recommended !== undefined && (
                                    <Typography variant="body1">
                                        Recommendation: {recommended ? "Recommended" : "Not recommended"}
                                    </Typography>
                                )}
                                {typeof score === "number" && (
                                    <Typography variant="body1">Score: {(score as number).toFixed(3)}</Typography>
                                )}
                                {typeof reason === "string" && (
                                    <Typography variant="body1" sx={{ whiteSpace: "pre-wrap", mt: 1 }}>
                                        {String(reason)}
                                    </Typography>
                                )}
                            </Box>
                        )}
                        {Array.isArray(similar) && similar.length > 0 && (
                            <Box>
                                <Typography variant="subtitle1" sx={{ fontWeight: 700, mb: 1 }}>
                                    Similar References
                                </Typography>
                                <Stack spacing={0.5}>
                                    {(similar as unknown[]).map((ref, idx) => (
                                        <Typography key={idx} variant="body2" sx={{ wordBreak: "break-word" }}>
                                            {typeof ref === "string" ? ref : formatJson(ref)}
                                        </Typography>
                                    ))}
                                </Stack>
                            </Box>
                        )}
                        <Divider />
                        <Box>
                            <Typography variant="subtitle1" sx={{ fontWeight: 700, mb: 1 }}>
                                Full Details
                            </Typography>
                            <Box component="pre" sx={{ m: 0, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
                                {formatJson(details)}
                            </Box>
                        </Box>
                    </Stack>
                </DialogContent>
                <DialogActions>
                    <Button onClick={() => setOpen(false)}>Close</Button>
                </DialogActions>
            </Dialog>
        </>
    );
};

export default AIAttributeChip;


