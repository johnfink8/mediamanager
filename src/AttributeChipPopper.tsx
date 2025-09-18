import React, { useState, useEffect, useRef } from "react";
import { Popper, Paper, FormControl, InputLabel, Select, MenuItem, Button, ClickAwayListener, Snackbar, Alert, TextField, Box } from "@mui/material";
import type { SelectProps } from "@mui/material/Select";
import { graphql, useMutation } from "react-relay";
import { AttributeChipPopperCreateFilterRuleMutation } from "./__generated__/AttributeChipPopperCreateFilterRuleMutation.graphql";
import { OPERATORS, Operator } from "./constants";

interface AttributeChipPopperProps {
    open: boolean;
    anchorEl: HTMLElement | null;
    onClose: () => void;
    operator: string;
    setOperator: (op: string) => void;
    name: string;
    value: string;
    itemType: string;
    details?: Record<string, unknown> | null;
}

const CreateFilterRuleMutation = graphql`
    mutation AttributeChipPopperCreateFilterRuleMutation($input: FilterRuleInput!) {
        createFilterRule(data: $input) {
            ignoreItems {
                id
                nodes {
                    id
                    type
                    uid
                    title
                    checkedTitle
                    posterUrl
                    attributes {
                        key
                        values
                    }
                }
            }
            filterRules {
                id
                nodes {
                    id
                    itemType
                    attribute
                    operator
                    value
                    enabled
                }
            }
        }
    }
`;

const AttributeChipPopper: React.FC<AttributeChipPopperProps> = ({
    open,
    anchorEl,
    onClose,
    operator,
    setOperator,
    name,
    value,
    itemType,
    details,
}) => {
    const [commit, isInFlight] = useMutation<AttributeChipPopperCreateFilterRuleMutation>(CreateFilterRuleMutation);
    const [snackbar, setSnackbar] = useState<{ open: boolean; message: string; severity: "success" | "error" }>({ open: false, message: "", severity: "success" });
    const [ignoreNextClickAway, setIgnoreNextClickAway] = useState(false);
    const [localValue, setLocalValue] = useState(value);
    const valueInputRef = useRef<HTMLInputElement>(null);

    useEffect(() => {
        if (open) {
            setLocalValue(value);
            setTimeout(() => valueInputRef.current?.focus(), 100);
        }
    }, [open, value]);

    const handleIgnore = () => {
        commit({
            variables: {
                input: {
                    itemType: itemType,
                    attribute: name,
                    operator: operator,
                    value: String(localValue),
                    enabled: true,
                },
            },
            updater: (store) => {
                // Update IgnoreItemList
                const payload = store.getRootField("createFilterRule");
                if (!payload) return;
                const ignoreItems = payload.getLinkedRecord("ignoreItems");
                if (ignoreItems) {
                    const id = ignoreItems.getValue("id") as string;
                    const nodes = ignoreItems.getLinkedRecords("nodes");
                    if (id && nodes) {
                        const ignoreList = store.get(id);
                        if (ignoreList) {
                            ignoreList.setLinkedRecords(nodes, "nodes");
                        }
                    }
                }
                // Update FilterRuleList
                const filterRules = payload.getLinkedRecord("filterRules");
                if (filterRules) {
                    const id = filterRules.getValue("id") as string;
                    const nodes = filterRules.getLinkedRecords("nodes");
                    if (id && nodes) {
                        const ruleList = store.get(id);
                        if (ruleList) {
                            ruleList.setLinkedRecords(nodes, "nodes");
                        }
                    }
                }
            },
            onCompleted: () => {
                setSnackbar({ open: true, message: `Rule added: ${name} ${OPERATORS.find(o => o.value === operator)?.label || operator} ${localValue}` , severity: "success" });
                onClose();
            },
            onError: (err) => {
                setSnackbar({ open: true, message: err.message, severity: "error" });
            },
        });
    };

    const handleOperatorChange:SelectProps["onChange"] = (event) => {
        setOperator(event.target.value as string);
    };
    const ops = OPERATORS;

    // Custom click away handler to ignore clicks inside MUI Select menu
    const handleClickAway = (event: MouseEvent | TouchEvent) => {
        if (ignoreNextClickAway) {
            setIgnoreNextClickAway(false);
            return;
        }
        const target = event.target as HTMLElement;
        if (
            target.closest('.MuiPopover-root') ||
            target.closest('.MuiMenu-root')
        ) {
            return;
        }
        onClose();
    };

    const handleValueKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
        if (e.key === "Enter" && localValue.trim() !== "") {
            handleIgnore();
        }
    };

    return (
        <>
            <Popper open={open} anchorEl={anchorEl} placement="bottom-start" style={{ zIndex: 1300 }}>
                <ClickAwayListener onClickAway={handleClickAway}>
                    <Paper
                        sx={{
                            p: 2,
                            borderRadius: 2,
                            border: '2px solid #1976d2',
                            color: '#222',
                            boxShadow: 3,
                            minWidth: 260,
                            maxWidth: 340,
                            bgcolor: '#fff',
                        }}
                        elevation={4}
                    >
                        <Box display="flex" flexDirection="column" gap={2}>
                            {details ? (
                                <Box sx={{ p: 1, bgcolor: '#f9f9f9', borderRadius: 1, border: '1px solid #eee' }}>
                                    <Box sx={{ fontWeight: 600, mb: 0.5 }}>Details</Box>
                                    <Box component="pre" sx={{ whiteSpace: 'pre-wrap', m: 0, fontSize: 12 }}>
                                        {JSON.stringify(details, null, 2)}
                                    </Box>
                                </Box>
                            ) : null}
                            <FormControl fullWidth size="small">
                                <InputLabel id="operator-select-label">Operator</InputLabel>
                                <Select
                                    labelId="operator-select-label"
                                    value={operator}
                                    label="Operator"
                                    onChange={handleOperatorChange}
                                    disabled={isInFlight}
                                    onOpen={() => setIgnoreNextClickAway(true)}
                                >
                                    {ops.map((op: Operator) => (
                                        <MenuItem key={op.value} value={op.value}>{op.label}</MenuItem>
                                    ))}
                                </Select>
                            </FormControl>
                            <TextField
                                label="Value"
                                value={localValue}
                                onChange={e => setLocalValue(e.target.value)}
                                onKeyDown={handleValueKeyDown}
                                inputRef={valueInputRef}
                                size="small"
                                disabled={isInFlight}
                                sx={{ bgcolor: '#fff' }}
                            />
                            <Button
                                onClick={handleIgnore}
                                color="secondary"
                                size="small"
                                disabled={isInFlight || localValue.trim() === ""}
                                variant="contained"
                                sx={{ alignSelf: 'flex-end', mt: 1 }}
                            >
                                Add rule: {name} {ops.find((o: Operator) => o.value === operator)?.label.toLowerCase() || operator} {localValue}
                            </Button>
                        </Box>
                    </Paper>
                </ClickAwayListener>
            </Popper>
            <Snackbar
                open={snackbar.open}
                autoHideDuration={3000}
                onClose={() => setSnackbar({ ...snackbar, open: false })}
                anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
            >
                <Alert onClose={() => setSnackbar({ ...snackbar, open: false })} severity={snackbar.severity} sx={{ width: '100%' }}>
                    {snackbar.message}
                </Alert>
            </Snackbar>
        </>
    );
};

export default AttributeChipPopper; 