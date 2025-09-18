import React, { useState, useEffect } from "react";
import { graphql, useQueryLoader, usePreloadedQuery, useMutation, PreloadedQuery } from "react-relay";
import {
    Table,
    TableBody,
    TableCell,
    TableContainer,
    TableHead,
    TableRow,
    Paper,
    Button,
    Switch,
    TextField,
    Box,
    IconButton,
    CircularProgress,
} from "@mui/material";
import DeleteIcon from "@mui/icons-material/Delete";
import { FilterRulesQuery as FilterRulesQueryType} from "./__generated__/FilterRulesQuery.graphql";
import { FilterRulesDeleteMutation } from "./__generated__/FilterRulesDeleteMutation.graphql";
import { FilterRulesCreateMutation } from "./__generated__/FilterRulesCreateMutation.graphql";

const FilterRulesQuery = graphql`
    query FilterRulesQuery {
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
`;

const CreateFilterRuleMutation = graphql`
    mutation FilterRulesCreateMutation($input: FilterRuleInput!) {
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

const DeleteFilterRuleMutation = graphql`
    mutation FilterRulesDeleteMutation($id: ID!) {
        deleteFilterRule(id: $id) {
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

const OPERATORS = ["eq", "neq", "lt", "gt", "in", "not_in", "contains", "not_contains"];

const FilterRulesTable: React.FC<{ queryRef: PreloadedQuery<FilterRulesQueryType> }> = ({ queryRef }) => {
    const data = usePreloadedQuery<FilterRulesQueryType>(FilterRulesQuery, queryRef);
    const [createRule, _creating] = useMutation<FilterRulesCreateMutation>(CreateFilterRuleMutation);
    const [deleteRule, _deleting] = useMutation<FilterRulesDeleteMutation>(DeleteFilterRuleMutation);
    const [form, setForm] = useState({
        attribute: "",
        operator: "eq",
        value: "",
        enabled: true,
        itemType: "mv",
    });

    const handleFormChange = (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => {
        setForm({ ...form, [e.target.name]: e.target.value });
    };

    const handleAdd = () => {
        createRule({
            variables: { input: form },
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
            onCompleted: () => setForm({ attribute: "", operator: "eq", value: "", enabled: true, itemType: "mv" }),
        });
    };

    const handleDelete = (id: string) => {
        deleteRule({
            variables: { id },
            updater: (store) => {
                // Update IgnoreItemList
                const payload = store.getRootField("deleteFilterRule");
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
        });
    };

    return (
        <Box sx={{ maxWidth: 700, margin: "auto", mt: 4 }}>
            <h2>Filter Rules</h2>
            <Box sx={{ display: "flex", gap: 2, mb: 2 }}>
                <TextField
                    label="Attribute"
                    name="attribute"
                    value={form.attribute}
                    onChange={handleFormChange}
                    size="small"
                />
                <TextField
                    select
                    label="Operator"
                    name="operator"
                    value={form.operator}
                    onChange={handleFormChange}
                    SelectProps={{ native: true } as any}
                    size="small"
                >
                    {OPERATORS.map((op) => (
                        <option key={op} value={op}>{op}</option>
                    ))}
                </TextField>
                <TextField
                    label="Value"
                    name="value"
                    value={form.value}
                    onChange={handleFormChange}
                    size="small"
                />
                <Button variant="contained" onClick={handleAdd} disabled={!form.attribute || !form.value}>
                    Add Rule
                </Button>
            </Box>
            <TableContainer component={Paper}>
                <Table>
                    <TableHead>
                        <TableRow>
                            <TableCell>Attribute</TableCell>
                            <TableCell>Operator</TableCell>
                            <TableCell>Value</TableCell>
                            <TableCell>Enabled</TableCell>
                            <TableCell>Actions</TableCell>
                        </TableRow>
                    </TableHead>
                    <TableBody>
                        {data.filterRules.nodes.map((rule) => (
                            <TableRow key={rule.id}>
                                <TableCell>{rule.attribute}</TableCell>
                                <TableCell>{rule.operator}</TableCell>
                                <TableCell>{rule.value}</TableCell>
                                <TableCell>
                                    <Switch checked={rule.enabled} disabled color="primary" />
                                </TableCell>
                                <TableCell>
                                    <IconButton onClick={() => handleDelete(rule.id)} size="small">
                                        <DeleteIcon />
                                    </IconButton>
                                </TableCell>
                            </TableRow>
                        ))}
                    </TableBody>
                </Table>
            </TableContainer>
        </Box>
    );
};

const FilterRules: React.FC = () => {
    const [queryRef, loadQuery] = useQueryLoader<FilterRulesQueryType>(FilterRulesQuery);
    useEffect(() => {
        loadQuery({});
    }, [loadQuery]);

    if (!queryRef) {
        return <Box sx={{ display: "flex", justifyContent: "center", mt: 4 }}><CircularProgress /></Box>;
    }
    return <FilterRulesTable queryRef={queryRef} />;
};

export default FilterRules; 